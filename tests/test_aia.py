from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

import linkcheck.aia as aia
from cryptography.x509.verification import Store

# Certs built offline with synthetic keys - a leaf issued by an intermediate issued
# by a self-signed root, mirroring the real-world "server didn't send the
# intermediate" shape this module recovers from. Each non-root cert's AIA extension
# points at a fake URL a MockTransport handler serves the next cert up from.

ROOT_URL = "https://aia.test/root.der"
INTERMEDIATE_URL = "https://aia.test/intermediate.der"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    *, subject_cn, issuer_cn, issuer_key, subject_key=None, aia_url=None, is_ca=False, dns_name=None
):
    """A minimally realistic cert - cryptography's x509.verification path builder
    enforces the same structural requirements a real CA would (BasicConstraints,
    key identifiers, a leaf's hostname living in subjectAltName rather than just its
    CN), so a fixture missing any of these fails verification for reasons unrelated
    to whatever the test is actually checking.
    """
    subject_key = subject_key or issuer_key
    now = datetime(2026, 1, 1, tzinfo=UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject_cn))
        .issuer_name(_name(issuer_cn))
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False
        )
    )
    if is_ca:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    if dns_name is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False
        )
    if aia_url is not None:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.CA_ISSUERS,
                        x509.UniformResourceIdentifier(aia_url),
                    )
                ]
            ),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _make_chain():
    root_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())

    root_cert = _cert(
        subject_cn="Test Root CA", issuer_cn="Test Root CA", issuer_key=root_key, is_ca=True
    )
    intermediate_cert = _cert(
        subject_cn="Test Intermediate CA",
        issuer_cn="Test Root CA",
        issuer_key=root_key,
        subject_key=intermediate_key,
        aia_url=ROOT_URL,
        is_ca=True,
    )
    leaf_cert = _cert(
        subject_cn="leaf.test",
        issuer_cn="Test Intermediate CA",
        issuer_key=intermediate_key,
        subject_key=leaf_key,
        aia_url=INTERMEDIATE_URL,
        dns_name="leaf.test",
    )
    return root_cert, intermediate_cert, leaf_cert


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_chase_completes_a_two_hop_missing_chain():
    root_cert, intermediate_cert, leaf_cert = _make_chain()

    def handler(request):
        if str(request.url) == ROOT_URL:
            return httpx.Response(200, content=root_cert.public_bytes(Encoding.DER))
        if str(request.url) == INTERMEDIATE_URL:
            return httpx.Response(200, content=intermediate_cert.public_bytes(Encoding.DER))
        raise AssertionError(f"unexpected AIA fetch: {request.url}")

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client,
            "leaf.test",
            443,
            leaf_cert_der=leaf_cert.public_bytes(Encoding.DER),
            trusted_roots=Store([root_cert]),
        )

    assert ctx is not None


@pytest.mark.asyncio
async def test_chase_stops_as_soon_as_the_chain_validates_even_via_a_non_self_signed_root():
    # Regression case: childrensmuseum.org's real chain has a root
    # ("Sectigo Public Server Authentication Root R46") that's directly trusted by
    # modern trust stores but is *also* cross-signed by an older, unrelated legacy
    # root for back-compat - so its own `issuer` field doesn't equal its `subject`.
    # A self-signed heuristic would keep chasing past this point trying to reach a
    # literal self-signed cert (and can wander into a mismatched/broken legacy
    # cross-sign chain doing so); chase() must stop the moment the chain-so-far
    # actually validates, however that root's own issuer field reads.
    upstream_key = ec.generate_private_key(ec.SECP256R1())
    cross_signed_root_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())

    # Directly trusted (it's in trusted_roots below) despite its own issuer field
    # naming a different, legacy CA - exactly Root R46's real-world shape.
    cross_signed_root = _cert(
        subject_cn="Trusted Root, Cross-Signed",
        issuer_cn="Legacy CA We Must Never Fetch",
        issuer_key=upstream_key,
        subject_key=cross_signed_root_key,
        aia_url="https://aia.test/must-not-fetch.der",
        is_ca=True,
    )
    intermediate_cert = _cert(
        subject_cn="Test Intermediate CA",
        issuer_cn="Trusted Root, Cross-Signed",
        issuer_key=cross_signed_root_key,
        subject_key=intermediate_key,
        aia_url=ROOT_URL,  # never fetched - verification succeeds one hop earlier
        is_ca=True,
    )
    leaf_cert = _cert(
        subject_cn="leaf.test",
        issuer_cn="Test Intermediate CA",
        issuer_key=intermediate_key,
        subject_key=leaf_key,
        aia_url=INTERMEDIATE_URL,
        dns_name="leaf.test",
    )

    def handler(request):
        if str(request.url) == INTERMEDIATE_URL:
            return httpx.Response(200, content=intermediate_cert.public_bytes(Encoding.DER))
        raise AssertionError(f"must not chase past the trusted root: {request.url}")

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client,
            "leaf.test",
            443,
            leaf_cert_der=leaf_cert.public_bytes(Encoding.DER),
            trusted_roots=Store([cross_signed_root]),
        )

    assert ctx is not None


@pytest.mark.asyncio
async def test_chase_gives_up_if_ca_issuer_url_fetch_fails():
    _, _, leaf_cert = _make_chain()

    def handler(request):
        return httpx.Response(404)

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client, "leaf.test", 443, leaf_cert_der=leaf_cert.public_bytes(Encoding.DER)
        )

    assert ctx is None


@pytest.mark.asyncio
async def test_chase_returns_none_when_leaf_has_no_aia_extension():
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    # issuer != subject (not self-signed) but no AIA extension - nothing to fetch,
    # and this isn't a chain-completion problem chase() can do anything about
    leaf_cert = _cert(
        subject_cn="leaf.test", issuer_cn="Unknown CA", issuer_key=root_key, subject_key=leaf_key
    )

    async def handler(request):
        raise AssertionError("should never fetch anything")

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client, "leaf.test", 443, leaf_cert_der=leaf_cert.public_bytes(Encoding.DER)
        )

    assert ctx is None


@pytest.mark.asyncio
async def test_chase_returns_none_for_an_already_self_signed_leaf():
    root_key = ec.generate_private_key(ec.SECP256R1())
    self_signed = _cert(subject_cn="leaf.test", issuer_cn="leaf.test", issuer_key=root_key)

    async def handler(request):
        raise AssertionError("should never fetch anything")

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client, "leaf.test", 443, leaf_cert_der=self_signed.public_bytes(Encoding.DER)
        )

    assert ctx is None


@pytest.mark.asyncio
async def test_chase_refuses_a_cyclical_aia_reference():
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    # points at itself forever - must not loop/trust blindly
    leaf_cert = _cert(
        subject_cn="leaf.test",
        issuer_cn="Some CA",
        issuer_key=root_key,
        subject_key=leaf_key,
        aia_url=INTERMEDIATE_URL,
    )

    def handler(request):
        return httpx.Response(200, content=leaf_cert.public_bytes(Encoding.DER))

    async with _mock_client(handler) as client:
        ctx = await aia.chase(
            client, "leaf.test", 443, leaf_cert_der=leaf_cert.public_bytes(Encoding.DER)
        )

    assert ctx is None
