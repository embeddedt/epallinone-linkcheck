"""HTTP link checking: classification, backoff scheduling, and concurrent execution."""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import sqlite3
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from linkcheck import aia
from linkcheck.config import (
    BROKEN_RECHECK_DAYS,
    CHECK_AIA_CHASE,
    CHECK_HTTPS_UPGRADE,
    CHECK_ONESHOT_POLL_SECONDS,
    HEALTHY_RECHECK_DAYS,
    RECHECK_JITTER_FRACTION,
    UNCONFIRMED_RETRY_MINUTES,
    USER_AGENT,
    exclusion_clause,
)

logger = logging.getLogger(__name__)

# error_type buckets. Not a closed set - ERROR_OTHER is a deliberate catch-all for
# anything that doesn't cleanly match one of the specific cases below, so we never
# mislabel an ambiguous failure as one we're actually confident about.
ERROR_TIMEOUT = "timeout"
ERROR_DNS = "dns_error"
ERROR_CONNECTION_REFUSED = "connection_refused"
ERROR_TOO_MANY_REDIRECTS = "too_many_redirects"
ERROR_BAD_SSL_CERT = "bad_ssl_cert"
ERROR_OTHER = "other"

STATUS_OK = "ok"
STATUS_BROKEN = "broken"
STATUS_UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class CheckResult:
    http_status: int | None
    error_type: str | None
    response_time_ms: int


def classify(http_status: int | None, error_type: str | None) -> str:
    """Map a raw check outcome to ok | broken | unreachable - the ONLY place "broken"
    is defined (raw outcomes are stored regardless, so extending it later reclassifies
    history rather than re-checking).

    404 (Not Found) and 410 (Gone) are the two definitively-dead statuses. 403 and 5xx
    are deliberately left as `ok`: a 403 is very often bot-blocking a URL a student's
    browser reaches fine, and a 5xx is usually a transient server hiccup - flagging
    either here would mostly manufacture false positives.
    """
    if error_type is not None:
        return STATUS_UNREACHABLE
    if http_status in (404, 410):
        return STATUS_BROKEN
    return STATUS_OK


def outcome(http_status: int | None, error_type: str | None) -> str:
    """One-token, human-facing summary of a raw check outcome: the error type if the
    request failed at the network level, otherwise the HTTP status code. Shared by the
    CLI, the worker log line, and the reports so they all spell it the same way.
    """
    return error_type if error_type is not None else str(http_status)


def _classify_connect_error(exc: BaseException) -> str:
    """Best-effort distinction between DNS failures, refused/reset connections, and bad
    TLS certs.

    httpx raises a single ConnectError for all of these; the underlying socket/ssl-level
    exception (if any) is somewhere in __cause__/__context__. ssl.SSLCertVerificationError
    (expired/self-signed/hostname-mismatch certs) is checked ahead of the generic
    socket.gaierror/ConnectionRefusedError cases and kept distinct from ERROR_OTHER's
    catch-all bucket of other ssl.SSLError variants (protocol/handshake failures unrelated
    to the cert itself), since a bad cert is a specific, actionable thing to flag on a
    course link. Falls back to ERROR_OTHER rather than guessing when nothing matches.
    """
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return ERROR_BAD_SSL_CERT
        if isinstance(cause, socket.gaierror):
            return ERROR_DNS
        if isinstance(cause, ConnectionRefusedError):
            return ERROR_CONNECTION_REFUSED
        cause = cause.__cause__ or cause.__context__
    return ERROR_OTHER


def _aia_retry_client(ctx: ssl.SSLContext) -> httpx.AsyncClient:
    """Split out from _fetch_via_aia_chase so tests can substitute a
    MockTransport-backed client for the retry instead of a real one."""
    return httpx.AsyncClient(verify=ctx)


async def _fetch_via_aia_chase(url: str) -> CheckResult | None:
    """After a bad_ssl_cert connect error, try to recover by fetching the server's
    missing intermediate certificate(s) ourselves (see linkcheck.aia) and retrying
    once against a completed chain. Returns None - falling back to the original
    bad_ssl_cert result - if AIA chasing can't complete the chain, or the retry
    still fails for any reason (including a *different* cert problem, e.g. the leaf
    itself being expired/self-signed, that AIA chasing was never going to fix).
    """
    parsed = httpx.URL(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    async with httpx.AsyncClient() as aia_client:
        ctx = await aia.chase(aia_client, parsed.host, port)
    if ctx is None:
        return None

    start = time.monotonic()
    try:
        async with _aia_retry_client(ctx) as retry_client:
            async with retry_client.stream(
                "GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            ) as response:
                return CheckResult(
                    response.status_code, None, int((time.monotonic() - start) * 1000)
                )
    except httpx.RequestError:
        return None


async def _fetch(client: httpx.AsyncClient, url: str) -> CheckResult:
    """One GET attempt against url. Streams the response and closes after headers -
    never downloads the full body, since we only care about the status code.
    """
    start = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        async with client.stream(
            "GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        ) as response:
            return CheckResult(response.status_code, None, elapsed_ms())
    except httpx.TooManyRedirects:
        return CheckResult(None, ERROR_TOO_MANY_REDIRECTS, elapsed_ms())
    except httpx.TimeoutException:
        return CheckResult(None, ERROR_TIMEOUT, elapsed_ms())
    except httpx.ConnectError as exc:
        error_type = _classify_connect_error(exc)
        if CHECK_AIA_CHASE and error_type == ERROR_BAD_SSL_CERT:
            chased = await _fetch_via_aia_chase(url)
            if chased is not None:
                return chased
        return CheckResult(None, error_type, elapsed_ms())
    except httpx.RequestError:
        return CheckResult(None, ERROR_OTHER, elapsed_ms())
    except ValueError:
        # Malformed URLs (e.g. "http:/example.com/..." with a single slash) aren't
        # rejected up front by httpx.URL - they only blow up later, deep in stdlib
        # urllib, when the client happens to have accumulated cookies and tries to
        # build a cookie header for the request. That raises a bare ValueError
        # instead of an httpx.RequestError, so it needs its own catch here - without
        # it, the exception escapes to the caller, record_check never runs, and the
        # link's next_check_at never advances, so it gets reclaimed and re-fails on
        # every single poll forever instead of settling into normal backoff.
        return CheckResult(None, ERROR_OTHER, elapsed_ms())


async def check_link(client: httpx.AsyncClient, url: str) -> CheckResult:
    """Check a single URL, mirroring the HTTPS-upgrade behavior every major browser now
    defaults to (Firefox's HTTPS-First since v136, Chrome's "Always Use Secure
    Connections" rolling out through 2026, Edge's Automatic HTTPS since v120 - see
    notes.md): a plain http:// URL is tried over https first, falling back to the
    literal http:// request only if the https attempt fails at the connection level
    (DNS/refused/TLS/timeout) - never merely because it returns a bad status, since a
    browser doesn't retry over http for that either. Without this, a link authored as
    http:// that 404s only because of a stale http-only redirect gets misreported as
    broken even though every real visitor's browser silently lands on a working https
    page and never sees the failure.

    Governed by config.CHECK_HTTPS_UPGRADE - off checks every URL exactly as stored.
    """
    if not CHECK_HTTPS_UPGRADE or not url.startswith("http://"):
        return await _fetch(client, url)

    https_result = await _fetch(client, "https://" + url[len("http://") :])
    if https_result.error_type is None:
        return https_result
    return await _fetch(client, url)


def _jittered_days(days: int) -> timedelta:
    """+/-RECHECK_JITTER_FRACTION on a recheck interval, so links confirmed together in
    the same batch (e.g. a crawl run) don't stay locked in a synchronized recheck cohort
    that reproduces the same spike of due links every cycle forever."""
    factor = 1 + random.uniform(-RECHECK_JITTER_FRACTION, RECHECK_JITTER_FRACTION)
    return timedelta(days=days * factor)


@dataclass(frozen=True)
class LinkState:
    status: str
    consecutive_failures: int


@dataclass(frozen=True)
class UpdatedLinkState:
    status: str
    consecutive_failures: int
    next_check_at: datetime


def next_state(previous: LinkState, result: CheckResult, now: datetime) -> UpdatedLinkState:
    """Confirm-before-flagging backoff: a link only flips to broken/unreachable after
    UNCONFIRMED_RETRY_MINUTES worth of consecutive failures, ruling out a transient
    blip. Once confirmed, it settles into a slower steady-state recheck - no need to
    hammer something already known to be down, just periodically confirm it's still
    down (or that it's recovered).
    """
    classification = classify(result.http_status, result.error_type)

    if classification == STATUS_OK:
        return UpdatedLinkState(
            status=STATUS_OK,
            consecutive_failures=0,
            next_check_at=now + _jittered_days(HEALTHY_RECHECK_DAYS),
        )

    failures = previous.consecutive_failures + 1
    if failures <= len(UNCONFIRMED_RETRY_MINUTES):
        return UpdatedLinkState(
            status=previous.status,  # not confirmed yet
            consecutive_failures=failures,
            next_check_at=now + timedelta(minutes=UNCONFIRMED_RETRY_MINUTES[failures - 1]),
        )

    # Confirmed. Clamp the counter at the confirm threshold so a link that stays broken
    # for months doesn't grow it without bound - past this point it only ever needs to
    # read as "confirmed" (>= threshold), and an unbounded counter would skew the
    # consecutive_failures-DESC report ordering by mere age rather than severity.
    return UpdatedLinkState(
        status=classification,
        consecutive_failures=min(failures, len(UNCONFIRMED_RETRY_MINUTES) + 1),
        next_check_at=now + _jittered_days(BROKEN_RECHECK_DAYS),
    )


@dataclass(frozen=True)
class DueLink:
    id: int
    url: str
    host: str
    status: str
    consecutive_failures: int


def get_due_links(conn: sqlite3.Connection, now: datetime, batch_size: int) -> list[DueLink]:
    """Due links, ignoring per-domain admission entirely - a plain read, not a claim.
    Used for inspection/reporting; the check phase itself uses claim_checkable_links.
    """
    exclude_clause, exclude_params = exclusion_clause("host")
    rows = conn.execute(
        f"""
        SELECT id, url, host, status, consecutive_failures FROM links
        WHERE next_check_at <= :now
          AND EXISTS (SELECT 1 FROM page_links WHERE page_links.link_id = links.id)
          {exclude_clause}
        ORDER BY next_check_at
        LIMIT :batch_size
        """,
        {"now": now.isoformat(), "batch_size": batch_size, **exclude_params},
    ).fetchall()
    return [
        DueLink(
            id=row["id"],
            url=row["url"],
            host=row["host"],
            status=row["status"],
            consecutive_failures=row["consecutive_failures"],
        )
        for row in rows
    ]


def pull_forward_broken_links(conn: sqlite3.Connection, now: datetime) -> int:
    """Set next_check_at to now for every confirmed broken/unreachable link that isn't
    already due, so the next check cycle reconsiders all of them immediately instead
    of waiting out whatever recheck interval was in effect when each was last
    confirmed (e.g. after tightening BROKEN_RECHECK_DAYS, which only affects
    scheduling decisions made from that point on - see next_state). Returns the number
    of links pulled forward.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE links SET next_check_at = :now WHERE status IN (:broken, :unreachable) AND next_check_at > :now",
            {"now": now.isoformat(), "broken": STATUS_BROKEN, "unreachable": STATUS_UNREACHABLE},
        )
    return cursor.rowcount


@dataclass(frozen=True)
class AdmissionControl:
    """Per-domain admission-control tuning, always applied together (see
    claim_checkable_links). Bundled so the check entry points take one object instead of
    threading the same three values through every signature and call site.
    """

    per_domain_limit: int
    min_interval_seconds: float
    stale_after_seconds: float


def _candidate_hosts(
    conn: sqlite3.Connection, now_iso: str, rate_threshold: str
) -> list[str]:
    # A flat "count of this host's current claims < per_domain_limit" check is the same
    # value for every due row belonging to that host - it doesn't rank them against each
    # other. So a host with a huge due backlog and early next_check_at values (bible.com,
    # in production) can pass that check on *every* one of its rows and fill the entire
    # LIMIT window before rows from any other host are even considered, starving hosts
    # that would otherwise be perfectly eligible.
    #
    # A single ranking query (e.g. ROW_NUMBER() OVER (PARTITION BY host ...)) would solve
    # the starvation problem but not cheaply: a window function has to materialize and
    # rank a host's *entire* due backlog before it can filter down to the per_domain_limit
    # rows that survive, so cost scales with that host's backlog size rather than with
    # per_domain_limit - measured at 176ms/poll for a single host with 50k due links
    # (returning only 3 rows), repeated as often as once a second while the backlog lasts.
    # So instead: find which hosts have any claimable work with one cheap pass (DISTINCT
    # over the due/eligible/rate-ok rows - no ranking, no per-host amplification), then
    # fetch each such host's earliest rows separately (see _gather_candidates).
    exclude_clause, exclude_params = exclusion_clause("links.host")
    return [
        row["host"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT links.host
            FROM links INDEXED BY idx_links_next_check
            LEFT JOIN domain_state ON domain_state.host = links.host
            WHERE links.next_check_at <= :now
              AND EXISTS (SELECT 1 FROM page_links WHERE page_links.link_id = links.id)
              AND NOT EXISTS (
                  SELECT 1 FROM domain_claims WHERE domain_claims.link_id = links.id
              )
              AND (domain_state.last_request_started_at IS NULL
                   OR domain_state.last_request_started_at <= :rate_threshold)
              {exclude_clause}
            """,
            {"now": now_iso, "rate_threshold": rate_threshold, **exclude_params},
        ).fetchall()
    ]


def _host_inflight_counts(
    conn: sqlite3.Connection, hosts: list[str]
) -> dict[str, int]:
    if not hosts:
        return {}
    placeholders = ",".join("?" for _ in hosts)
    return {
        row["host"]: row["n"]
        for row in conn.execute(
            f"""
            SELECT host, COUNT(*) AS n FROM domain_claims
            WHERE host IN ({placeholders})
            GROUP BY host
            """,
            hosts,
        ).fetchall()
    }


def _gather_candidates(
    conn: sqlite3.Connection, hosts: list[str], now_iso: str, per_domain_limit: int, limit: int
) -> list[sqlite3.Row]:
    """The earliest-due, still-claimable rows for each candidate host, capped per host at
    its remaining concurrency and globally at `limit`. Each per-host fetch is a bounded
    indexed seek (idx_links_host_next_check), so cost is independent of backlog depth.

    Re-applies exclusion_clause() here even though _candidate_hosts already filtered
    at the host level - a link-text rule (e.g. source-citation) excludes individual
    links, not whole hosts, so a host can still have both claimable and excluded links
    mixed together.
    """
    exclude_clause, exclude_params = exclusion_clause("host")
    host_inflight = _host_inflight_counts(conn, hosts)
    candidates: list[sqlite3.Row] = []
    for host in hosts:
        remaining_capacity = per_domain_limit - host_inflight.get(host, 0)
        if remaining_capacity <= 0:
            continue
        candidates.extend(
            conn.execute(
                f"""
                SELECT id, url, host, status, consecutive_failures, next_check_at
                FROM links INDEXED BY idx_links_host_next_check
                WHERE host = :host
                  AND next_check_at <= :now
                  AND EXISTS (SELECT 1 FROM page_links WHERE page_links.link_id = links.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM domain_claims WHERE domain_claims.link_id = links.id
                  )
                  {exclude_clause}
                ORDER BY next_check_at
                LIMIT :remaining_capacity
                """,
                {"host": host, "now": now_iso, "remaining_capacity": remaining_capacity, **exclude_params},
            ).fetchall()
        )
    candidates.sort(key=lambda row: row["next_check_at"])
    return candidates[:limit]


def _try_claim(
    conn: sqlite3.Connection, row: sqlite3.Row, now_iso: str, rate_threshold: str, per_domain_limit: int
) -> bool:
    """Optimistically claim one candidate as a pair of conditional writes. Returns True
    if the claim was taken; a lost race just returns False and the row is left for a
    later poll (it's still due).
    """
    # Consume the host's rate slot only if the host also has concurrency room - otherwise
    # a claim about to be rejected for concurrency would still move
    # last_request_started_at forward, needlessly stalling the host for a full
    # min_interval without any request actually going out. On the plain INSERT path (a
    # host with no domain_state row yet) the host has never been requested, so it has no
    # domain_claims and concurrency room is guaranteed; the capacity guard only needs to
    # gate the DO UPDATE (existing-host) path.
    rate_claim = conn.execute(
        """
        INSERT INTO domain_state (host, last_request_started_at)
        VALUES (:host, :now)
        ON CONFLICT(host) DO UPDATE SET last_request_started_at = :now
        WHERE domain_state.last_request_started_at <= :rate_threshold
          AND (SELECT COUNT(*) FROM domain_claims
               WHERE domain_claims.host = :host) < :per_domain_limit
        """,
        {
            "host": row["host"],
            "now": now_iso,
            "rate_threshold": rate_threshold,
            "per_domain_limit": per_domain_limit,
        },
    )
    if rate_claim.rowcount != 1:
        # Host is rate-limited, at its concurrency cap, or another candidate for it
        # already took this round's slot - either way, nothing was consumed.
        return False

    conn.execute(
        "INSERT INTO domain_claims (host, link_id, claimed_at) VALUES (:host, :link_id, :now)",
        {"host": row["host"], "link_id": row["id"], "now": now_iso},
    )
    return True


def claim_checkable_links(
    conn: sqlite3.Connection, now: datetime, limit: int, *, admission: AdmissionControl
) -> list[DueLink]:
    """Select and claim up to `limit` due links that are safe to check right now.

    "Safe" is entirely a property of the join against domain_state/domain_claims (see
    schema.sql): not already claimed (anti-join on domain_claims.link_id - so the same
    link can never be claimed twice while a check is in flight for it), the link's host
    isn't already at its concurrency limit, and enough time has passed since the last
    request start to that host. Because ineligible links are excluded by the query
    itself rather than discovered after the fact, whatever comes back is inherently
    host-diverse and immediately actionable - no in-process shuffling needed to avoid
    piling workers up on one busy domain.

    Claims are optimistic (see _try_claim): the read and the claims happen as separate
    statements, so two candidates that share a host could both pass the read but only
    some still fit once claiming starts. A lost race just means that candidate is skipped
    this round - it's still due, so it'll be a candidate again on the next poll.
    """
    now_iso = now.isoformat()
    rate_threshold = (now - timedelta(seconds=admission.min_interval_seconds)).isoformat()
    stale_before = (now - timedelta(seconds=admission.stale_after_seconds)).isoformat()

    with conn:
        # Abandoned claims from a crashed process eventually age out here, rather than
        # being reset on startup - which would incorrectly clobber a genuinely active
        # claim held by another linkcheck invocation running against the same DB.
        conn.execute("DELETE FROM domain_claims WHERE claimed_at < ?", (stale_before,))

        hosts = _candidate_hosts(conn, now_iso, rate_threshold)
        candidates = _gather_candidates(conn, hosts, now_iso, admission.per_domain_limit, limit)

        claimed: list[DueLink] = []
        for row in candidates:
            if _try_claim(conn, row, now_iso, rate_threshold, admission.per_domain_limit):
                claimed.append(
                    DueLink(
                        id=row["id"],
                        url=row["url"],
                        host=row["host"],
                        status=row["status"],
                        consecutive_failures=row["consecutive_failures"],
                    )
                )
        return claimed


def release_claim(conn: sqlite3.Connection, link_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM domain_claims WHERE link_id = ?", (link_id,))


def record_check(
    conn: sqlite3.Connection, link: DueLink, result: CheckResult, now: datetime
) -> UpdatedLinkState:
    classification = classify(result.http_status, result.error_type)
    updated = next_state(
        LinkState(status=link.status, consecutive_failures=link.consecutive_failures),
        result,
        now,
    )
    with conn:
        conn.execute(
            """
            INSERT INTO link_checks
                (link_id, checked_at, http_status, error_type, response_time_ms, classified_broken)
            VALUES (:link_id, :checked_at, :http_status, :error_type, :response_time_ms, :classified_broken)
            """,
            {
                "link_id": link.id,
                "checked_at": now.isoformat(),
                "http_status": result.http_status,
                "error_type": result.error_type,
                "response_time_ms": result.response_time_ms,
                "classified_broken": 1 if classification == STATUS_BROKEN else 0,
            },
        )
        conn.execute(
            """
            UPDATE links SET
                last_checked_at = :now,
                next_check_at = :next_check_at,
                last_http_status = :http_status,
                last_error_type = :error_type,
                consecutive_failures = :consecutive_failures,
                status = :status
            WHERE id = :id
            """,
            {
                "now": now.isoformat(),
                "next_check_at": updated.next_check_at.isoformat(),
                "http_status": result.http_status,
                "error_type": result.error_type,
                "consecutive_failures": updated.consecutive_failures,
                "status": updated.status,
                "id": link.id,
            },
        )
    return updated


OnResult = Callable[[DueLink, CheckResult, UpdatedLinkState], None]


async def _check_claimed_link(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    link: DueLink,
    on_result: OnResult | None,
) -> None:
    """Check one already-claimed link and release its claim when done, regardless of
    outcome. The claim only gates admission; it's unrelated to `next_check_at`, which
    is what actually determines when the link is due again.
    """
    try:
        result = await check_link(client, link.url)
        updated = record_check(conn, link, result, datetime.now(UTC))
        if on_result is not None:
            on_result(link, result, updated)
    except Exception:
        # Don't let one unexpected failure (e.g. a DB error) take down the poll loop -
        # log it and let the next poll try again.
        logger.exception("Unexpected error checking %s", link.url)
    finally:
        release_claim(conn, link.id)


def _claim_and_dispatch(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    tasks: set[asyncio.Task],
    *,
    room: int,
    admission: AdmissionControl,
    on_result: OnResult | None,
) -> list[DueLink]:
    """Claim up to `room` due links and fire each off as its own check task, tracking it
    in `tasks` (self-removing on completion). Returns what was claimed so the caller can
    pace itself. Shared by both the one-shot and continuous poll loops.
    """
    if room <= 0:
        return []
    claimed = claim_checkable_links(conn, datetime.now(UTC), room, admission=admission)
    for link in claimed:
        task = asyncio.create_task(_check_claimed_link(conn, client, link, on_result))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return claimed


async def check_due_links(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    *,
    target: int,
    global_limit: int,
    admission: AdmissionControl,
    poll_interval: float = CHECK_ONESHOT_POLL_SECONDS,
    on_result: OnResult | None = None,
) -> int:
    """Check up to `target` currently-due links, then stop once either that many have
    been claimed or nothing more can be claimed and nothing is left in flight (a
    one-shot, best-effort run - used by the `linkcheck check` CLI command).

    Some due links may be skipped if their domain is genuinely rate-limited or at
    capacity right now, including by another linkcheck invocation sharing the same
    database - run it again to pick up whatever's eligible by then. Returns the number
    actually checked.
    """
    tasks: set[asyncio.Task] = set()
    claimed_total = 0

    while claimed_total < target:
        room = min(global_limit - len(tasks), target - claimed_total)
        claimed = _claim_and_dispatch(
            conn, client, tasks, room=room, admission=admission, on_result=on_result
        )
        claimed_total += len(claimed)

        if not claimed:
            if not tasks:
                break  # nothing claimable, nothing in flight - genuinely done for now
            await asyncio.sleep(poll_interval)

    await asyncio.gather(*tasks, return_exceptions=True)
    return claimed_total


async def run_continuous_checks(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    *,
    global_limit: int,
    admission: AdmissionControl,
    refill_size: int,
    poll_interval: float,
    fast_poll_interval: float,
    on_result: OnResult | None = None,
) -> None:
    """Continuously check due links until stop_event is set, claiming whatever's
    eligible on each poll and firing it off immediately.

    No persistent worker pool or in-memory queue: eligibility (per-domain concurrency
    and rate limit) is entirely a property of the claim query against domain_state/
    domain_claims (see claim_checkable_links), so there's nothing to coordinate
    in-process beyond a plain count of how many checks this process currently has
    outstanding, capping how much gets claimed per poll. A slow or heavily
    rate-limited domain only ever affects the handful of tasks actually assigned to
    it - polling for fresh, unrelated work is never blocked on it, unlike a
    batch-pull-then-wait-for-everything model.
    """
    tasks: set[asyncio.Task] = set()

    while not stop_event.is_set():
        room = min(refill_size, max(0, global_limit - len(tasks)))
        claimed = _claim_and_dispatch(
            conn, client, tasks, room=room, admission=admission, on_result=on_result
        )

        # Idle (nothing in flight, nothing claimed) backs off to the slow interval;
        # any active work polls again soon so completions get topped up promptly.
        wait = poll_interval if (not tasks and not claimed) else fast_poll_interval
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
        except TimeoutError:
            pass

    if tasks:
        logger.info(
            "Waiting for %d in-flight check%s to finish...",
            len(tasks),
            "" if len(tasks) == 1 else "s",
        )
    await asyncio.gather(*tasks, return_exceptions=True)
