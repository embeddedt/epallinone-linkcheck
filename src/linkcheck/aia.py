"""AIA (Authority Information Access) chasing.

Some servers serve a valid, unexpired leaf certificate but an incomplete or
mismatched intermediate chain (a misconfiguration, not a bad cert) - OpenSSL-based
clients like httpx then can't build a path to a trusted root and report a cert
verification failure. Real browsers paper over this: a cert's AIA extension
carries a "CA Issuers" URL pointing at the intermediate that issued it, and
browsers fetch whatever's missing from there rather than failing the connection.
This module does the same thing, so a link every visitor's browser reaches fine
isn't misreported as broken (see checker.ERROR_BAD_SSL_CERT / config.CHECK_AIA_CHASE
and notes.md for the childrensmuseum.org case this exists for).

Fetching the leaf cert unverified (`_leaf_cert_der`) is safe: it's only used to read
the plaintext AIA URL out of the cert, never as a trust decision. The chain built
from what AIA hands back is only trusted because it's verified for real - twice
over: once here via cryptography's path validator (deciding when to stop chasing),
and again by OpenSSL on the retried request, using the SSLContext this module
returns.

Chasing to a literal self-signed root would be simpler but is wrong: some chains
cross-sign a still-self-signed root through an older, differently-named root for
back-compat (this is exactly what Sectigo's chain for childrensmuseum.org does),
so the *next* cert up from a missing intermediate isn't self-signed even though
it's already a trusted root in its own right. Re-validating against the trust
store after every fetch stops as soon as the chain is actually complete, however
many hops that takes - never walking further than necessary.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import warnings

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.utils import CryptographyDeprecationWarning
from cryptography.x509.oid import AuthorityInformationAccessOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from linkcheck.config import AIA_CHASE_MAX_HOPS, AIA_CHASE_TIMEOUT_SECONDS

# Parsed once at import time from certifi's bundled root store - static for the life
# of the process, so there's no reason to re-parse it on every chase() call. A
# handful of certifi's older roots (GoDaddy/Starfield/SECOM/HARICA) carry a
# zero serial number, technically non-conformant with RFC 5280 but harmless and not
# ours to fix - suppressed here rather than left to print on every process start.
with open(certifi.where(), "rb") as _f, warnings.catch_warnings():
    warnings.simplefilter("ignore", CryptographyDeprecationWarning)
    _TRUSTED_ROOTS = Store(x509.load_pem_x509_certificates(_f.read()))


def _leaf_cert_der(host: str, port: int, timeout: float) -> bytes:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if der is None:
        raise ssl.SSLError("server presented no certificate")
    return der


def _ca_issuer_urls(cert: x509.Certificate) -> list[str]:
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return []
    return [
        desc.access_location.value
        for desc in aia
        if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS
    ]


def _parse_issuer_response(content: bytes) -> list[x509.Certificate]:
    """The CA Issuers URL almost always serves one DER certificate, occasionally a
    PKCS7 bundle (.p7c) with several - try both rather than assuming."""
    try:
        return [x509.load_der_x509_certificate(content)]
    except ValueError:
        pass
    try:
        return list(pkcs7.load_der_pkcs7_certificates(content))
    except ValueError:
        return []


async def chase(
    client: httpx.AsyncClient,
    host: str,
    port: int,
    *,
    leaf_cert_der: bytes | None = None,
    trusted_roots: Store | None = None,
) -> ssl.SSLContext | None:
    """Best-effort: walk each cert's AIA "CA Issuers" URL up toward a trusted root,
    fetching whatever intermediate(s) the server should have sent but didn't, and
    stopping as soon as the chain-so-far actually validates against the trust
    store. Returns an SSLContext trusting the system roots plus the recovered
    intermediates, or None if the chain can't be completed that way - no AIA
    extension (nothing to fetch, the failure is something else), a fetch failed, or
    it still doesn't validate within AIA_CHASE_MAX_HOPS. `leaf_cert_der` and
    `trusted_roots` let callers (tests) inject the starting cert and the trust
    store instead of making a real TLS connection / trusting the real system roots.
    """
    if leaf_cert_der is None:
        try:
            leaf_cert_der = await asyncio.to_thread(
                _leaf_cert_der, host, port, AIA_CHASE_TIMEOUT_SECONDS
            )
        except (OSError, ssl.SSLError):
            return None

    store = trusted_roots if trusted_roots is not None else _TRUSTED_ROOTS
    leaf = x509.load_der_x509_certificate(leaf_cert_der)
    try:
        verifier = PolicyBuilder().store(store).build_server_verifier(x509.DNSName(host))
    except ValueError:
        return None  # host isn't a valid DNS name (e.g. a bare IP) - nothing we can do

    cert = leaf
    intermediates: list[x509.Certificate] = []
    seen_urls: set[str] = set()

    for _ in range(AIA_CHASE_MAX_HOPS):
        try:
            verifier.verify(leaf, intermediates)
            break  # chain-so-far already validates to a trusted root
        except VerificationError:
            pass

        urls = _ca_issuer_urls(cert)
        if not urls:
            return None  # nothing more to fetch and still not valid - can't help
        url = urls[0]
        if url in seen_urls:
            return None  # cyclical AIA reference - refuse to trust it
        seen_urls.add(url)
        try:
            response = await client.get(url, timeout=AIA_CHASE_TIMEOUT_SECONDS)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        fetched = _parse_issuer_response(response.content)
        if not fetched:
            return None
        intermediates.extend(fetched)
        cert = fetched[-1]
    else:
        return None  # exhausted the hop budget without the chain ever validating

    if not intermediates:
        return None  # already validated with nothing fetched - not a chain-completion issue

    ctx = ssl.create_default_context(cafile=certifi.where())
    pem = b"".join(c.public_bytes(Encoding.PEM) for c in intermediates)
    ctx.load_verify_locations(cadata=pem.decode("ascii"))
    return ctx
