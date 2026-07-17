"""Main process: crawl loop + check loop running concurrently against one DB."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx

from linkcheck import checker, crawler, db, report
from linkcheck.config import (
    CHECK_BATCH_SIZE,
    CHECK_FEEDER_FAST_POLL_SECONDS,
    CHECK_GLOBAL_CONCURRENCY,
    CHECK_LOOP_INTERVAL_SECONDS,
    CHECK_MAX_REDIRECTS,
    CHECK_PER_DOMAIN_CONCURRENCY,
    CHECK_PER_DOMAIN_MIN_INTERVAL_SECONDS,
    CHECK_PROGRESS_LOG_SECONDS,
    CHECK_STALE_CLAIM_SECONDS,
    CHECK_TIMEOUT_SECONDS,
    CRAWL_INTERVAL_HOURS,
    CRAWL_TIMEOUT_SECONDS,
    DASHBOARD_HTML_PATH,
    SITES,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

# A single sqlite3.Connection is shared between the crawl and check loops below (and,
# within the check loop, across every concurrently in-flight check task). That's safe
# without an explicit lock: everything here runs on one asyncio event loop (never
# truly concurrent Python execution), and every function that touches the connection
# (sync_course_page, claim_checkable_links, record_check, ...) is a plain synchronous
# function with no `await` inside it - so each DB operation runs to completion
# atomically before the event loop can switch to another task.


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def crawl_loop(conn: sqlite3.Connection, stop_event: asyncio.Event) -> None:
    """Recrawl all course pages for both sites, then wait for the next cycle.

    Runs once immediately on startup. A shutdown request is only honored between
    cycles, not mid-crawl - a full crawl is on the order of a hundred course-page
    fetches, so this keeps the loop simple without cancellation plumbing.
    """
    async with httpx.AsyncClient(
        timeout=CRAWL_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        while not stop_event.is_set():
            for site in SITES:
                try:
                    results = await crawler.crawl_site(conn, client, site)
                except Exception:
                    # A crawl touches the network, JSON parsing, and SQLite - any of
                    # which can fail for one site. Log and move on rather than letting
                    # it escape into asyncio.gather and take the check loop down with it.
                    logger.exception("Crawl failed for site %s", site.slug)
                    continue
                found = sum(1 for r in results if r.found)
                logger.info("%s: crawled %d/%d course pages", site.slug, found, len(results))
            await _sleep_or_stop(stop_event, CRAWL_INTERVAL_HOURS * 3600)


def _write_dashboard(conn: sqlite3.Connection, dashboard_path: str) -> None:
    summaries = report.get_site_summaries(conn)
    problem_links = report.get_problem_links(conn)
    watch_links = report.get_watch_links(conn)
    html = report.render_html_report(
        summaries, problem_links, watch_links, datetime.now(UTC).isoformat()
    )
    # Write-then-rename so a reader never loads a half-written file, and a crash
    # mid-write leaves the previous dashboard intact rather than a truncated one.
    path = Path(dashboard_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(html)
    os.replace(tmp, path)


async def check_loop(
    conn: sqlite3.Connection, stop_event: asyncio.Event, dashboard_path: str
) -> None:
    """Run the persistent check pool (see checker.run_continuous_checks) and, on a
    separate fixed cadence, log a summary of what it's done and regenerate the static
    dashboard - decoupled from the pool's own poll timing, since dashboard/log
    freshness doesn't need to track individual check completions.
    """
    stats = {"checked": 0, "not_ok": 0}

    def on_result(link, result, updated) -> None:
        stats["checked"] += 1
        if updated.status != "ok":
            stats["not_ok"] += 1
        if checker.classify(result.http_status, result.error_type) != checker.STATUS_OK:
            outcome = checker.outcome(result.http_status, result.error_type)
            logger.warning("check failed [%s] %s (status=%s)", outcome, link.url, updated.status)

    async with httpx.AsyncClient(
        timeout=CHECK_TIMEOUT_SECONDS, max_redirects=CHECK_MAX_REDIRECTS
    ) as client:
        pool_task = asyncio.create_task(
            checker.run_continuous_checks(
                conn,
                client,
                stop_event,
                global_limit=CHECK_GLOBAL_CONCURRENCY,
                admission=checker.AdmissionControl(
                    per_domain_limit=CHECK_PER_DOMAIN_CONCURRENCY,
                    min_interval_seconds=CHECK_PER_DOMAIN_MIN_INTERVAL_SECONDS,
                    stale_after_seconds=CHECK_STALE_CLAIM_SECONDS,
                ),
                refill_size=CHECK_BATCH_SIZE,
                poll_interval=CHECK_LOOP_INTERVAL_SECONDS,
                fast_poll_interval=CHECK_FEEDER_FAST_POLL_SECONDS,
                on_result=on_result,
            )
        )
        def refresh_dashboard() -> None:
            # Never let a report-query or template error kill the check loop (and, via
            # gather, its siblings) - the checks themselves matter more than the dashboard.
            try:
                _write_dashboard(conn, dashboard_path)
            except Exception:
                logger.exception("Failed to regenerate dashboard")

        try:
            refresh_dashboard()  # initial write, don't wait a full interval
            while not stop_event.is_set():
                await _sleep_or_stop(stop_event, CHECK_LOOP_INTERVAL_SECONDS)
                if stats["checked"]:
                    logger.info("Checked %d due links (%d not ok)", stats["checked"], stats["not_ok"])
                    stats["checked"] = 0
                    stats["not_ok"] = 0
                refresh_dashboard()
        finally:
            await pool_task


async def progress_loop(conn: sqlite3.Connection, stop_event: asyncio.Event) -> None:
    """Log the fraction of checkable links checked at least once, on its own fixed
    cadence - independent of check_loop's batch/dashboard timing, so there's a visible
    sign of life (and an ETA-able trend) even while a big backlog drains.
    """
    while not stop_event.is_set():
        await _sleep_or_stop(stop_event, CHECK_PROGRESS_LOG_SECONDS)
        if stop_event.is_set():
            break
        progress = report.get_check_progress(conn)
        logger.info(
            "Progress: %d/%d links checked (%.1f%%)",
            progress.checked,
            progress.total,
            progress.pct,
        )


async def run(db_path: str, dashboard_path: str = DASHBOARD_HTML_PATH) -> None:
    conn = db.connect(db_path)
    db.init_db(conn)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        # return_exceptions=True so an unexpected failure in one loop doesn't cancel the
        # others - which would otherwise let the `finally` close the shared connection out
        # from under check tasks still in flight. Each loop is individually resilient; this
        # is the backstop for anything that slips through.
        results = await asyncio.gather(
            crawl_loop(conn, stop_event),
            check_loop(conn, stop_event, dashboard_path),
            progress_loop(conn, stop_event),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error("A worker loop exited with an exception", exc_info=result)
    finally:
        conn.close()
