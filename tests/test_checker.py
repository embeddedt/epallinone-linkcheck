import http.client
import socket
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import linkcheck.checker
from linkcheck.checker import (
    Classification,
    CheckResult,
    LinkState,
    build_check_ssl_context,
    check_link,
    classify,
    next_state,
)
from linkcheck.config import (
    BROKEN_RECHECK_DAYS,
    HEALTHY_RECHECK_DAYS,
    RECHECK_JITTER_FRACTION,
    UNCONFIRMED_RETRY_MINUTES,
)

assert len(UNCONFIRMED_RETRY_MINUTES) >= 2  # the interval-index tests below assume this


def assert_next_check_within_jitter(actual: datetime, now: datetime, days: int) -> None:
    delta = actual - now
    lo = timedelta(days=days * (1 - RECHECK_JITTER_FRACTION))
    hi = timedelta(days=days * (1 + RECHECK_JITTER_FRACTION))
    assert lo <= delta <= hi, f"{delta} not within +/-{RECHECK_JITTER_FRACTION:.0%} of {days} days"


# --- build_check_ssl_context() ---


def test_build_check_ssl_context_still_verifies_certs():
    # Lowering the key-size floor (SECLEVEL) must not also disable certificate
    # verification - those are orthogonal knobs and only the former should change.
    ctx = build_check_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_build_check_ssl_context_permits_a_1024_bit_dh_cipher():
    # SECLEVEL=1's whole point is accepting handshakes SECLEVEL=2 (OpenSSL's default)
    # rejects for using a <2048-bit ephemeral DH key - assert the level actually
    # dropped by checking a DHE cipher survived the set_ciphers() call.
    ctx = build_check_ssl_context()
    names = {cipher["name"] for cipher in ctx.get_ciphers()}
    assert any(name.startswith("DHE-") for name in names)


# --- classify() ---


def _result(http_status, error_type=None, **kwargs):
    return CheckResult(http_status, error_type, 10, **kwargs)


def test_classify_404_is_broken():
    assert classify("https://x.test/a", _result(404)) == Classification("broken", None)


def test_classify_410_gone_is_broken():
    assert classify("https://x.test/a", _result(410)) == Classification("broken", None)


def test_classify_200_is_ok():
    assert classify("https://x.test/a", _result(200)) == Classification("ok", None)


def test_classify_5xx_is_broken():
    assert classify("https://x.test/a", _result(500)) == Classification("broken", None)
    assert classify("https://x.test/a", _result(503)) == Classification("broken", None)


def test_classify_403_is_ok_for_now():
    # 403 (often bot-blocking a scraper that a student's browser reaches fine) is
    # deliberately left ok - this is what changes if the definition of "broken" is
    # extended later
    assert classify("https://x.test/a", _result(403)) == Classification("ok", None)


def test_classify_network_error_is_unreachable_regardless_of_status():
    assert classify("https://x.test/a", _result(None, "timeout")) == Classification(
        "unreachable", None
    )


# --- classify() rot detection (see test_rot.py for the heuristics themselves) ---


def test_classify_200_with_rot_signal_is_broken_with_reason():
    # a homepage-redirect verdict on an otherwise-200 response - the exact
    # heuristics are covered in test_rot.py, this just checks classify() wires
    # rot.detect_rot's verdict into a Classification correctly
    result = _result(200, final_url="https://x.test/")
    assert classify("https://x.test/deep/lesson", result) == Classification(
        "broken", "homepage_redirect"
    )


def test_classify_skips_rot_detection_when_disabled(monkeypatch):
    monkeypatch.setattr(linkcheck.checker, "CHECK_ROT_DETECTION", False)
    result = _result(200, final_url="https://x.test/")
    assert classify("https://x.test/deep/lesson", result) == Classification("ok", None)


# --- classify() YouTube oEmbed video-unavailable check ---


def test_classify_youtube_403_is_broken_as_video_unavailable():
    # a bare 403 is normally left ok (see test_classify_other_error_status_is_ok_for_now)
    # - for a YouTube video url specifically it means "private video", not bot-blocking
    result = _result(403)
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "broken", "video_unavailable"
    )


def test_classify_youtube_404_gets_the_richer_video_unavailable_reason():
    # still broken either way, but the video-specific reason wins over the plain
    # 404 branch's bare None reason
    result = _result(404)
    assert classify("https://youtu.be/dQw4w9WgXcQ", result) == Classification(
        "broken", "video_unavailable"
    )


def test_classify_youtube_401_stays_ok():
    # embedding disabled, not unwatchable - a real visitor can still watch it on
    # youtube.com itself
    result = _result(401)
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "ok", None
    )


def test_classify_non_youtube_404_is_unaffected():
    result = _result(404)
    assert classify("https://example.com/video", result) == Classification("broken", None)


def test_classify_skips_youtube_oembed_check_when_disabled(monkeypatch):
    monkeypatch.setattr(linkcheck.checker, "CHECK_YOUTUBE_OEMBED", False)
    result = _result(403)
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "ok", None
    )


def test_classify_skips_rot_detection_for_oembed_probed_video():
    # a 2xx oEmbed probe's final_url/title describe the oEmbed JSON resource, not
    # the watch url stored as the link - detect_rot must never run against that
    # mismatched pair. page_title is deliberately set to something that WOULD trip
    # soft_404 (a title-only heuristic, so the mismatched final_url doesn't matter
    # here) if detect_rot ran, proving the skip actually happens rather than just
    # coincidentally not firing.
    result = _result(
        200,
        final_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        page_title="Page Not Found",
    )
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "ok", None
    )


def test_classify_runs_rot_detection_for_youtube_url_when_oembed_disabled(monkeypatch):
    # with oEmbed routing off, a youtube url is fetched like any other page, so its
    # own rot signals ARE meaningful and detect_rot must still run
    monkeypatch.setattr(linkcheck.checker, "CHECK_YOUTUBE_OEMBED", False)
    result = _result(200, final_url="https://www.youtube.com/")
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "broken", "homepage_redirect"
    )


# --- next_state() backoff ---


def test_next_state_healthy_resets_failures_and_sets_long_recheck():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="broken", consecutive_failures=1),
        Classification("ok", None),
        now,
    )
    assert updated.status == "ok"
    assert updated.consecutive_failures == 0
    assert_next_check_within_jitter(updated.next_check_at, now, HEALTHY_RECHECK_DAYS)


def test_next_state_first_failure_is_unconfirmed():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=0),
        Classification("broken", None),
        now,
    )
    assert updated.status == "ok"  # not flipped yet
    assert updated.consecutive_failures == 1
    assert updated.next_check_at == now + timedelta(minutes=UNCONFIRMED_RETRY_MINUTES[0])


def test_next_state_confirms_after_enough_consecutive_failures():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    failures_before_confirm = len(UNCONFIRMED_RETRY_MINUTES)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=failures_before_confirm),
        Classification("broken", None),
        now,
    )
    assert updated.status == "broken"
    assert updated.consecutive_failures == failures_before_confirm + 1
    assert_next_check_within_jitter(updated.next_check_at, now, BROKEN_RECHECK_DAYS)


def test_next_state_confirmed_failure_count_is_clamped_at_threshold():
    # A link that stays broken across many rechecks must not grow consecutive_failures
    # without bound - it clamps at the confirm threshold once confirmed.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    confirmed = len(UNCONFIRMED_RETRY_MINUTES) + 1
    updated = next_state(
        LinkState(status="broken", consecutive_failures=confirmed + 5),
        Classification("broken", None),
        now,
    )
    assert updated.status == "broken"
    assert updated.consecutive_failures == confirmed


def test_next_state_second_unconfirmed_failure_uses_the_second_retry_interval():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=1),
        Classification("broken", None),
        now,
    )
    assert updated.status == "ok"  # still within the retry window, not confirmed
    assert updated.consecutive_failures == 2
    assert updated.next_check_at == now + timedelta(minutes=UNCONFIRMED_RETRY_MINUTES[1])


def test_next_state_confirms_unreachable_on_persistent_network_failure():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=len(UNCONFIRMED_RETRY_MINUTES)),
        Classification("unreachable", None),
        now,
    )
    # network-level failure confirms as unreachable, kept distinct from broken
    assert updated.status == "unreachable"
    assert_next_check_within_jitter(updated.next_check_at, now, BROKEN_RECHECK_DAYS)


def test_next_state_confirmed_broken_carries_the_rot_reason_into_status():
    # next_state itself doesn't touch broken_reason (that's persisted separately by
    # record_check) but the confirmed status must still be "broken" for a rot
    # verdict, exactly as for a plain 404 - confirm-before-flagging doesn't
    # distinguish between them.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    failures_before_confirm = len(UNCONFIRMED_RETRY_MINUTES)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=failures_before_confirm),
        Classification("broken", "homepage_redirect"),
        now,
    )
    assert updated.status == "broken"


def test_next_state_single_bad_check_does_not_immediately_flip_a_healthy_link():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=0),
        Classification("unreachable", None),
        now,
    )
    assert updated.status == "ok"
    assert updated.consecutive_failures == 1


# --- check_link() against a mock transport (no real network) ---


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_check_link_ok():
    async with _client(lambda request: httpx.Response(200)) as client:
        result = await check_link(client, "https://x.test/ok")
    assert result.http_status == 200
    assert result.error_type is None


@pytest.mark.asyncio
async def test_check_link_404():
    async with _client(lambda request: httpx.Response(404)) as client:
        result = await check_link(client, "https://x.test/missing")
    assert result.http_status == 404
    assert result.error_type is None


@pytest.mark.asyncio
async def test_check_link_timeout():
    def handler(request):
        raise httpx.ReadTimeout("boom", request=request)

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/slow")
    assert result.http_status is None
    assert result.error_type == "timeout"


@pytest.mark.asyncio
async def test_check_link_too_many_redirects():
    def handler(request):
        return httpx.Response(301, headers={"location": str(request.url)})

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/loop")
    assert result.http_status is None
    assert result.error_type == "too_many_redirects"


@pytest.mark.asyncio
async def test_check_link_connect_error_falls_back_to_other_without_a_known_cause():
    def handler(request):
        raise httpx.ConnectError("mystery failure", request=request)

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/down")
    assert result.error_type == "other"


@pytest.mark.asyncio
async def test_check_link_connect_error_dns_is_classified_from_gaierror_cause():
    def handler(request):
        raise httpx.ConnectError("name resolution failed", request=request) from socket.gaierror(
            -2, "Name or service not known"
        )

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/no-such-host")
    assert result.error_type == "dns_error"


@pytest.mark.asyncio
async def test_check_link_connect_error_refused_is_classified_from_cause():
    def handler(request):
        raise httpx.ConnectError("refused", request=request) from ConnectionRefusedError()

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/refused")
    assert result.error_type == "connection_refused"


@pytest.mark.asyncio
async def test_check_link_connect_error_bad_ssl_cert_is_classified_from_cause():
    def handler(request):
        raise httpx.ConnectError(
            "certificate verify failed", request=request
        ) from ssl.SSLCertVerificationError()

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/bad-cert")
    assert result.error_type == "bad_ssl_cert"


# --- AIA chase recovery from a bad_ssl_cert connect error ---
#
# aia.chase() itself is covered offline in test_aia.py; here we only need to check
# that check_link() wires a successful/failed chase into the right CheckResult. A
# dummy (not None) ssl.SSLContext stands in for a "chase succeeded" signal, and
# _aia_retry_client is monkeypatched so the retry hits a MockTransport instead of a
# real socket - see conftest.py for why the default (unpatched) path is already safe.


def _bad_cert_handler(request):
    raise httpx.ConnectError(
        "certificate verify failed", request=request
    ) from ssl.SSLCertVerificationError()


@pytest.mark.asyncio
async def test_check_link_recovers_via_aia_chase_when_retry_succeeds(monkeypatch):
    async def fake_chase(client, host, port, *, leaf_cert_der=None):
        return ssl.create_default_context()

    monkeypatch.setattr(linkcheck.checker.aia, "chase", fake_chase)
    monkeypatch.setattr(
        linkcheck.checker,
        "_aia_retry_client",
        lambda ctx: _client(lambda request: httpx.Response(200)),
    )

    async with _client(_bad_cert_handler) as client:
        result = await check_link(client, "https://x.test/bad-cert")

    assert result.http_status == 200
    assert result.error_type is None


@pytest.mark.asyncio
async def test_check_link_falls_back_to_bad_ssl_cert_when_chase_retry_still_fails(monkeypatch):
    async def fake_chase(client, host, port, *, leaf_cert_der=None):
        return ssl.create_default_context()

    def still_fails(request):
        raise httpx.ConnectError("still broken", request=request)

    monkeypatch.setattr(linkcheck.checker.aia, "chase", fake_chase)
    monkeypatch.setattr(linkcheck.checker, "_aia_retry_client", lambda ctx: _client(still_fails))

    async with _client(_bad_cert_handler) as client:
        result = await check_link(client, "https://x.test/bad-cert")

    assert result.error_type == "bad_ssl_cert"


@pytest.mark.asyncio
async def test_check_link_falls_back_to_bad_ssl_cert_when_chase_cannot_complete_chain(
    monkeypatch,
):
    async def chase_gives_up(client, host, port, *, leaf_cert_der=None):
        return None

    monkeypatch.setattr(linkcheck.checker.aia, "chase", chase_gives_up)

    async with _client(_bad_cert_handler) as client:
        result = await check_link(client, "https://x.test/bad-cert")

    assert result.error_type == "bad_ssl_cert"


@pytest.mark.asyncio
async def test_check_link_skips_aia_chase_entirely_when_disabled(monkeypatch):
    def chase_should_not_be_called(*args, **kwargs):
        raise AssertionError("chase() must not run when CHECK_AIA_CHASE is off")

    monkeypatch.setattr(linkcheck.checker, "CHECK_AIA_CHASE", False)
    monkeypatch.setattr(linkcheck.checker.aia, "chase", chase_should_not_be_called)

    async with _client(_bad_cert_handler) as client:
        result = await check_link(client, "https://x.test/bad-cert")

    assert result.error_type == "bad_ssl_cert"


# --- lenient http.client fallback recovery from a malformed-header RemoteProtocolError ---
#
# _lenient_get is monkeypatched at the check_link level (mirroring the AIA chase
# tests above) so the retry doesn't hit a real socket; _lenient_get's own redirect-
# following logic gets a direct unit test below with a fake http.client connection.


def _bad_header_handler(request):
    raise httpx.RemoteProtocolError("illegal header line: b'https 200 OK: '", request=request)


@pytest.mark.asyncio
async def test_check_link_recovers_via_lenient_http_client_when_retry_succeeds(monkeypatch):
    monkeypatch.setattr(
        linkcheck.checker,
        "_lenient_get",
        lambda url, timeout: linkcheck.checker._LenientResult(200, url, None, None),
    )

    async with _client(_bad_header_handler) as client:
        result = await check_link(client, "https://x.test/bad-header")

    assert result.http_status == 200
    assert result.error_type is None
    assert result.final_url == "https://x.test/bad-header"


@pytest.mark.asyncio
async def test_check_link_falls_back_to_other_when_lenient_retry_also_fails(monkeypatch):
    def still_fails(url, timeout):
        raise OSError("connection reset")

    monkeypatch.setattr(linkcheck.checker, "_lenient_get", still_fails)

    async with _client(_bad_header_handler) as client:
        result = await check_link(client, "https://x.test/bad-header")

    assert result.http_status is None
    assert result.error_type == "other"


@pytest.mark.asyncio
async def test_check_link_skips_lenient_fallback_entirely_when_disabled(monkeypatch):
    def should_not_be_called(url, timeout):
        raise AssertionError("_lenient_get must not run when CHECK_LENIENT_HTTP_FALLBACK is off")

    monkeypatch.setattr(linkcheck.checker, "CHECK_LENIENT_HTTP_FALLBACK", False)
    monkeypatch.setattr(linkcheck.checker, "_lenient_get", should_not_be_called)

    async with _client(_bad_header_handler) as client:
        result = await check_link(client, "https://x.test/bad-header")

    assert result.http_status is None
    assert result.error_type == "other"


class _FakeEmailHeaders:
    """Minimal stand-in for http.client.HTTPResponse.headers (an email.message.Message)
    - _read_lenient_body_sample only ever calls get_content_charset() on it."""

    def __init__(self, content_type: str):
        self._content_type = content_type

    def get_content_charset(self):
        if "charset=" in self._content_type:
            return self._content_type.split("charset=")[-1].strip()
        return None


class _FakeHttpResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self._headers = headers or {}
        self._body = body
        self.headers = _FakeEmailHeaders(self._headers.get("Content-Type", ""))

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, amt=None):
        if amt is None:
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:amt], self._body[amt:]
        return data

    def close(self):
        pass


class _FakeHttpConnection:
    """Stand-in for http.client.HTTPSConnection - records requested host+path pairs
    and returns canned responses in sequence, so _lenient_get's own redirect-following
    can be tested without a real socket."""

    calls: list[str] = []
    responses: list[_FakeHttpResponse] = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def request(self, method, path, headers=None):
        _FakeHttpConnection.calls.append(f"{self.host}{path}")

    def getresponse(self):
        return _FakeHttpConnection.responses.pop(0)

    def close(self):
        pass


def test_lenient_get_follows_redirects_and_returns_final_status(monkeypatch):
    _FakeHttpConnection.calls = []
    _FakeHttpConnection.responses = [
        _FakeHttpResponse(301, {"Location": "/next"}),
        _FakeHttpResponse(200),
    ]
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHttpConnection)

    result = linkcheck.checker._lenient_get("https://x.test/start", timeout=5)

    assert result.status == 200
    assert result.final_url == "https://x.test/next"
    assert _FakeHttpConnection.calls == ["x.test/start", "x.test/next"]


def test_lenient_get_gives_up_after_too_many_redirects(monkeypatch):
    _FakeHttpConnection.calls = []
    _FakeHttpConnection.responses = [
        _FakeHttpResponse(302, {"Location": "/start"}) for _ in range(50)
    ]
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHttpConnection)

    with pytest.raises(RuntimeError):
        linkcheck.checker._lenient_get("https://x.test/start", timeout=5)


def test_lenient_get_reads_capped_html_body_and_extracts_title_and_excerpt(monkeypatch):
    _FakeHttpConnection.calls = []
    _FakeHttpConnection.responses = [
        _FakeHttpResponse(
            200,
            {"Content-Type": "text/html; charset=utf-8"},
            body=b"<html><head><title>Page Not Found</title></head>"
            b"<body><script>ignored()</script><p>Sorry, nothing here.</p></body></html>",
        ),
    ]
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHttpConnection)

    result = linkcheck.checker._lenient_get("https://x.test/start", timeout=5)

    assert result.page_title == "Page Not Found"
    assert "sorry, nothing here" in result.body_excerpt
    assert "ignored()" not in result.body_excerpt


def test_lenient_get_skips_body_sample_for_non_html_content_type(monkeypatch):
    _FakeHttpConnection.calls = []
    _FakeHttpConnection.responses = [
        _FakeHttpResponse(200, {"Content-Type": "application/pdf"}, body=b"%PDF-1.4 ..."),
    ]
    monkeypatch.setattr(http.client, "HTTPSConnection", _FakeHttpConnection)

    result = linkcheck.checker._lenient_get("https://x.test/start", timeout=5)

    assert result.page_title is None
    assert result.body_excerpt is None


@pytest.mark.asyncio
async def test_check_link_bare_value_error_is_caught_as_other():
    # A malformed URL can make httpx raise a bare ValueError (deep in urllib cookie
    # handling) rather than an httpx.RequestError. It must be swallowed as `other`, not
    # propagate - otherwise record_check never runs and the link is reclaimed forever.
    def handler(request):
        raise ValueError("Invalid header value")

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/malformed")
    assert result.http_status is None
    assert result.error_type == "other"


# --- check_link() HTTPS-upgrade (mirrors browser default behavior, see notes.md) ---


@pytest.mark.asyncio
async def test_check_link_upgrades_http_to_https_when_https_works():
    def handler(request):
        assert request.url.scheme == "https"
        return httpx.Response(200)

    async with _client(handler) as client:
        result = await check_link(client, "http://x.test/page")
    assert result.http_status == 200


@pytest.mark.asyncio
async def test_check_link_does_not_fall_back_to_http_on_a_bad_https_status():
    # A real browser doesn't retry over http just because https answered with a 404 -
    # only a connection-level failure triggers the http fallback. So the upgraded
    # https:// request's 404 must be the final result, not masked by a second attempt.
    def handler(request):
        assert request.url.scheme == "https"
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await check_link(client, "http://x.test/gone")
    assert result.http_status == 404
    assert result.error_type is None


@pytest.mark.asyncio
async def test_check_link_falls_back_to_http_when_https_connect_fails():
    def handler(request):
        if request.url.scheme == "https":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    async with _client(handler) as client:
        result = await check_link(client, "http://x.test/http-only")
    assert result.http_status == 200
    assert result.error_type is None


@pytest.mark.asyncio
async def test_check_link_reports_http_failure_when_both_schemes_fail():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as client:
        result = await check_link(client, "http://x.test/down")
    assert result.http_status is None
    assert result.error_type == "other"


@pytest.mark.asyncio
async def test_check_link_leaves_https_urls_untouched():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        await check_link(client, "https://x.test/already-secure")
    assert seen_urls == ["https://x.test/already-secure"]


@pytest.mark.asyncio
async def test_check_link_upgrade_disabled_checks_http_url_as_is(monkeypatch):
    monkeypatch.setattr(linkcheck.checker, "CHECK_HTTPS_UPGRADE", False)
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await check_link(client, "http://x.test/plain")
    assert seen_urls == ["http://x.test/plain"]
    assert result.http_status == 404


# --- final_url / page_title / body_excerpt capture (_fetch / check_link) ---


@pytest.mark.asyncio
async def test_check_link_captures_final_url_and_title_after_redirect():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><head><title>Final Page</title></head></html>",
        )

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/start")

    assert result.final_url == "https://x.test/final"
    assert result.page_title == "Final Page"


@pytest.mark.asyncio
async def test_check_link_captures_final_url_even_on_404():
    # final_url is captured for every completed response, not just 2xx - useful
    # context on its own, and rot detection's own guard (see rot.detect_rot) is
    # what actually keeps it from being misused on a non-2xx status
    async with _client(lambda request: httpx.Response(404)) as client:
        result = await check_link(client, "https://x.test/missing")

    assert result.final_url == "https://x.test/missing"
    assert result.page_title is None


@pytest.mark.asyncio
async def test_check_link_extracts_body_excerpt_for_html_response():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><script>evil()</script><p>Hello World</p></body></html>",
        )

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/page")

    assert "hello world" in result.body_excerpt
    assert "evil" not in result.body_excerpt


@pytest.mark.asyncio
async def test_check_link_skips_body_sample_for_non_html_content_type():
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF fake")

    async with _client(handler) as client:
        result = await check_link(client, "https://x.test/file.pdf")

    assert result.page_title is None
    assert result.body_excerpt is None


# --- check_link() YouTube oEmbed routing ---


@pytest.mark.asyncio
async def test_check_link_youtube_watch_url_hits_oembed_endpoint():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"title": "Some Video"})

    async with _client(handler) as client:
        result = await check_link(client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert len(seen_urls) == 1
    assert seen_urls[0].startswith("https://www.youtube.com/oembed?")
    assert "url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ" in seen_urls[0]
    assert "format=json" in seen_urls[0]
    assert result.http_status == 200


@pytest.mark.asyncio
async def test_check_link_youtube_short_url_hits_oembed_endpoint():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"title": "Some Video"})

    async with _client(handler) as client:
        await check_link(client, "https://youtu.be/dQw4w9WgXcQ")

    assert seen_urls[0].startswith("https://www.youtube.com/oembed?")
    assert "url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ" in seen_urls[0]


@pytest.mark.asyncio
async def test_check_link_youtube_v_not_first_query_param_hits_oembed_endpoint():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"title": "Some Video"})

    async with _client(handler) as client:
        await check_link(client, "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ")

    assert "url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ" in seen_urls[0]


@pytest.mark.asyncio
async def test_check_link_youtube_oembed_401_leaves_status_401_and_classifies_ok():
    def handler(request):
        return httpx.Response(401)

    async with _client(handler) as client:
        result = await check_link(client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert result.http_status == 401
    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "ok", None
    )


@pytest.mark.asyncio
async def test_check_link_youtube_oembed_403_classifies_broken_video_unavailable():
    def handler(request):
        return httpx.Response(403)

    async with _client(handler) as client:
        result = await check_link(client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert classify("https://www.youtube.com/watch?v=dQw4w9WgXcQ", result) == Classification(
        "broken", "video_unavailable"
    )


@pytest.mark.asyncio
async def test_check_link_youtube_oembed_404_classifies_broken_video_unavailable():
    def handler(request):
        return httpx.Response(404)

    async with _client(handler) as client:
        result = await check_link(client, "https://youtu.be/dQw4w9WgXcQ")

    assert classify("https://youtu.be/dQw4w9WgXcQ", result) == Classification(
        "broken", "video_unavailable"
    )


@pytest.mark.asyncio
async def test_check_link_youtube_playlist_url_falls_through_to_normal_fetch():
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        await check_link(client, "https://www.youtube.com/playlist?list=PL123")

    assert seen_urls == ["https://www.youtube.com/playlist?list=PL123"]


@pytest.mark.asyncio
async def test_check_link_skips_oembed_routing_when_disabled(monkeypatch):
    monkeypatch.setattr(linkcheck.checker, "CHECK_YOUTUBE_OEMBED", False)
    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        await check_link(client, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    assert seen_urls == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


# --- _read_body_sample() / _extract_title_and_excerpt() ---


@pytest.mark.asyncio
async def test_read_body_sample_caps_bytes_read(monkeypatch):
    monkeypatch.setattr(linkcheck.checker, "CHECK_BODY_SAMPLE_BYTES", 10)
    response = httpx.Response(200, headers={"content-type": "text/html"}, content=b"0123456789ABCDEFGHIJ")

    sample = await linkcheck.checker._read_body_sample(response)

    assert sample == "0123456789"


@pytest.mark.asyncio
async def test_read_body_sample_skips_non_2xx_status():
    response = httpx.Response(404, headers={"content-type": "text/html"}, content=b"<html></html>")
    assert await linkcheck.checker._read_body_sample(response) is None


@pytest.mark.asyncio
async def test_read_body_sample_skips_non_html_content_type():
    response = httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4")
    assert await linkcheck.checker._read_body_sample(response) is None


def test_extract_title_strips_whitespace_and_unescapes_entities():
    html_sample = "<html><head><title>  Hello &amp;  World  </title></head></html>"
    title, _ = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert title == "Hello & World"


def test_extract_title_none_when_missing():
    title, _ = linkcheck.checker._extract_title_and_excerpt("<html><body>no title here</body></html>")
    assert title is None


def test_extract_title_caps_length():
    long_title = "x" * 1000
    html_sample = f"<html><title>{long_title}</title></html>"
    title, _ = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert len(title) == linkcheck.checker.MAX_TITLE_CHARS


def test_extract_title_ignores_title_tag_inside_script(monkeypatch):
    # a regex-based <title> scan (the old implementation) matches this literally -
    # a real FP vector for SPA boilerplate that sets document.title via a template
    # literal containing the string "<title>...</title>"
    html_sample = (
        "<html><head>"
        '<script>var x = "<title>Fake Script Title</title>";</script>'
        "<title>Real Page Title</title>"
        "</head><body><p>Hello World</p></body></html>"
    )
    title, excerpt = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert title == "Real Page Title"
    assert "fake script title" not in (excerpt or "")


def test_extract_title_ignores_title_tag_inside_svg():
    # an inline SVG's own <title> is tooltip text for the graphic, not the page's
    # title - must not be picked up when there's no real <title> in <head>
    html_sample = (
        "<html><head></head><body>"
        "<svg><title>Icon description</title><rect/></svg>"
        "<p>Hello World</p>"
        "</body></html>"
    )
    title, excerpt = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert title is None
    assert "icon description" not in (excerpt or "")


def test_extract_title_does_not_leak_into_body_excerpt():
    # the title's own words must not be double-counted in the body excerpt (e.g.
    # parking()'s title+body haystack)
    html_sample = "<html><head><title>Page Not Found</title></head><body><p>Sorry, nothing here.</p></body></html>"
    title, excerpt = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert title == "Page Not Found"
    assert "page not found" not in excerpt
    assert "sorry, nothing here" in excerpt


def test_extract_body_excerpt_strips_script_and_style_and_lowercases():
    html_sample = (
        "<html><head><style>.a{color:red}</style></head>"
        "<body><script>evil()</script><p>Hello World</p></body></html>"
    )
    _, excerpt = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert "hello world" in excerpt
    assert "evil" not in excerpt
    assert "color" not in excerpt


def test_extract_body_excerpt_caps_length():
    html_sample = f"<html><body>{'x' * 10000}</body></html>"
    _, excerpt = linkcheck.checker._extract_title_and_excerpt(html_sample)
    assert len(excerpt) == linkcheck.checker.MAX_BODY_EXCERPT_CHARS
