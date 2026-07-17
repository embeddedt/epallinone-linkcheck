import socket
import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import linkcheck.checker
from linkcheck.checker import (
    CheckResult,
    LinkState,
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


# --- classify() ---


def test_classify_404_is_broken():
    assert classify(404, None) == "broken"


def test_classify_410_gone_is_broken():
    assert classify(410, None) == "broken"


def test_classify_200_is_ok():
    assert classify(200, None) == "ok"


def test_classify_other_error_status_is_ok_for_now():
    # 404 and 410 are the only broken statuses today; 403 (often bot-blocking) and 5xx
    # (often transient) are deliberately left ok - this is what changes if the
    # definition of "broken" is extended later
    assert classify(500, None) == "ok"
    assert classify(403, None) == "ok"


def test_classify_network_error_is_unreachable_regardless_of_status():
    assert classify(None, "timeout") == "unreachable"


# --- next_state() backoff ---


def test_next_state_healthy_resets_failures_and_sets_long_recheck():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="broken", consecutive_failures=1),
        CheckResult(200, None, 10),
        now,
    )
    assert updated.status == "ok"
    assert updated.consecutive_failures == 0
    assert_next_check_within_jitter(updated.next_check_at, now, HEALTHY_RECHECK_DAYS)


def test_next_state_first_failure_is_unconfirmed():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=0),
        CheckResult(404, None, 10),
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
        CheckResult(404, None, 10),
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
        CheckResult(404, None, 10),
        now,
    )
    assert updated.status == "broken"
    assert updated.consecutive_failures == confirmed


def test_next_state_second_unconfirmed_failure_uses_the_second_retry_interval():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=1),
        CheckResult(404, None, 10),
        now,
    )
    assert updated.status == "ok"  # still within the retry window, not confirmed
    assert updated.consecutive_failures == 2
    assert updated.next_check_at == now + timedelta(minutes=UNCONFIRMED_RETRY_MINUTES[1])


def test_next_state_confirms_unreachable_on_persistent_network_failure():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=len(UNCONFIRMED_RETRY_MINUTES)),
        CheckResult(None, "timeout", 10),
        now,
    )
    # network-level failure confirms as unreachable, kept distinct from broken
    assert updated.status == "unreachable"
    assert_next_check_within_jitter(updated.next_check_at, now, BROKEN_RECHECK_DAYS)


def test_next_state_single_bad_check_does_not_immediately_flip_a_healthy_link():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    updated = next_state(
        LinkState(status="ok", consecutive_failures=0),
        CheckResult(None, "timeout", 10),
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
