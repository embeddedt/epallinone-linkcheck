"""Command-line entry points for linkcheck."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import click
import httpx

from linkcheck import checker, crawler, db, report, scheduler
from linkcheck.config import (
    CHECK_GLOBAL_CONCURRENCY,
    CHECK_MAX_REDIRECTS,
    CHECK_PER_DOMAIN_CONCURRENCY,
    CHECK_PER_DOMAIN_MIN_INTERVAL_SECONDS,
    CHECK_STALE_CLAIM_SECONDS,
    CHECK_TIMEOUT_SECONDS,
    CRAWL_TIMEOUT_SECONDS,
    DASHBOARD_HTML_PATH,
    DEFAULT_DB_PATH,
    SITES,
)


@click.group()
def main() -> None:
    """linkcheck: crawl curriculum pages and check their external links."""


@main.command("init-db")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
def init_db_command(db_path: str) -> None:
    """Create tables (if missing) and sync site config into the database."""
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    click.echo(f"Initialized database at {db_path}")


@main.command("discover-courses")
def discover_courses_command() -> None:
    """Fetch each site's course index page and print the course pages found.

    Read-only sanity check against the live sites; does not touch the database.
    """

    async def run() -> None:
        async with httpx.AsyncClient(timeout=CRAWL_TIMEOUT_SECONDS) as client:
            for site in SITES:
                courses = await crawler.discover_courses_for_site(client, site)
                click.echo(f"{site.slug}: {len(courses)} course pages")
                for course in courses:
                    click.echo(f"  {course.title!r} -> {course.url}")

    asyncio.run(run())


@main.command("crawl-preview")
@click.option(
    "--limit",
    default=3,
    show_default=True,
    help="Number of course pages per site to fetch and extract, for a quick sanity check.",
)
def crawl_preview_command(limit: int) -> None:
    """Fetch a handful of real course pages per site and print extracted links.

    Read-only sanity check against the live sites; does not touch the database.
    """

    async def run() -> None:
        async with httpx.AsyncClient(timeout=CRAWL_TIMEOUT_SECONDS) as client:
            for site in SITES:
                courses = await crawler.discover_courses_for_site(client, site)
                click.echo(f"{site.slug}: {len(courses)} course pages discovered")
                for course in courses[:limit]:
                    page = await crawler.fetch_course_page(client, site, course)
                    if page is None:
                        click.echo(f"  {course.title!r} -> {course.url} : NOT FOUND")
                        continue
                    links = crawler.extract_links(page.html, page.canonical_url, site.base_url)
                    click.echo(f"  {page.title!r}: {len(links)} external links")
                    for link in links[:5]:
                        click.echo(f"    [{link.day_context}] {link.text!r} -> {link.url}")

    asyncio.run(run())


@main.command("crawl")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    help="Only crawl the first N course pages per site, and skip pruning pages that "
    "dropped out of reachability (for testing) - a limited crawl doesn't walk the full "
    "graph, so it can't tell what's actually still linked.",
)
def crawl_command(db_path: str, limit: int | None) -> None:
    """Crawl all course pages for both sites, and whatever they transitively link to
    on the same site, syncing the resulting page graph and its external links into the
    database."""
    conn = db.connect(db_path)
    db.init_db(conn)

    async def run() -> None:
        async with httpx.AsyncClient(timeout=CRAWL_TIMEOUT_SECONDS) as client:
            for site in SITES:
                results = await crawler.crawl_site(conn, client, site, limit=limit)
                found = sum(1 for r in results if r.found)
                course_count = sum(1 for r in results if r.kind == "course")
                other_count = len(results) - course_count
                click.echo(
                    f"{site.slug}: crawled {found}/{len(results)} pages "
                    f"({course_count} course, {other_count} other)"
                )
                for result in results:
                    label = result.title or result.slug
                    if not result.found:
                        click.echo(f"  SKIP (not found): {label!r} -> {result.url}")
                        continue
                    suffix = " (unchanged)" if result.unchanged else ""
                    click.echo(f"  [{result.kind}] {label!r}: {result.link_count} external links synced{suffix}")

    try:
        asyncio.run(run())
    finally:
        conn.close()


@main.command("check")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
@click.option(
    "--batch-size",
    default=50,
    show_default=True,
    help="Maximum number of due links to check in this run.",
)
def check_command(db_path: str, batch_size: int) -> None:
    """Check up to N currently-due links and record results.

    Best-effort: some due links may be skipped this run if their domain is genuinely
    rate-limited or at capacity right now (including by a concurrently-running
    `linkcheck run` sharing this database) - rerun to pick up whatever's eligible by
    then.
    """
    conn = db.connect(db_path)

    def on_result(link, result, updated) -> None:
        if checker.classify(result.http_status, result.error_type) == checker.STATUS_OK:
            return
        outcome = checker.outcome(result.http_status, result.error_type)
        click.echo(f"  [{updated.status:>11}] {outcome:>5} {link.url}")

    async def run() -> None:
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT_SECONDS, max_redirects=CHECK_MAX_REDIRECTS
        ) as client:
            checked = await checker.check_due_links(
                conn,
                client,
                target=batch_size,
                global_limit=CHECK_GLOBAL_CONCURRENCY,
                admission=checker.AdmissionControl(
                    per_domain_limit=CHECK_PER_DOMAIN_CONCURRENCY,
                    min_interval_seconds=CHECK_PER_DOMAIN_MIN_INTERVAL_SECONDS,
                    stale_after_seconds=CHECK_STALE_CLAIM_SECONDS,
                ),
                on_result=on_result,
            )
        click.echo(f"Checked {checked} links")

    try:
        asyncio.run(run())
    finally:
        conn.close()


@main.command("requeue-broken")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
def requeue_broken_command(db_path: str) -> None:
    """Pull next_check_at forward to now for every broken/unreachable link.

    Use this after changing the recheck cadence (e.g. BROKEN_RECHECK_DAYS) to apply it
    to links already scheduled under the old value, instead of waiting for their
    existing schedule to catch up on its own.
    """
    conn = db.connect(db_path)
    try:
        count = checker.pull_forward_broken_links(conn, datetime.now(UTC))
        click.echo(f"Requeued {count} broken/unreachable links for immediate recheck")
    finally:
        conn.close()


@main.command("report")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
@click.option(
    "--html",
    "html_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Also render a static HTML dashboard to this path.",
)
def report_command(db_path: str, html_path: str | None) -> None:
    """Print a text report of current link status, and optionally a static HTML dashboard."""
    conn = db.connect(db_path)
    try:
        summaries = report.get_site_summaries(conn)
        problem_links = report.get_problem_links(conn)
        watch_links = report.get_watch_links(conn)
        click.echo(report.render_text_report(summaries, problem_links, watch_links))
        if html_path:
            html = report.render_html_report(problem_links, watch_links)
            out_path = Path(html_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(html)
            click.echo(f"\nWrote HTML dashboard to {html_path}")
    finally:
        conn.close()


@main.command("run")
@click.option(
    "--db-path",
    default=DEFAULT_DB_PATH,
    show_default=True,
    help="Path to the SQLite database file.",
)
@click.option(
    "--dashboard-path",
    default=DASHBOARD_HTML_PATH,
    show_default=True,
    help="Where to (re)write the static HTML dashboard after each check cycle.",
)
def run_command(db_path: str, dashboard_path: str) -> None:
    """Run the background worker: crawl loop + check loop, until interrupted (Ctrl-C)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs an INFO line per request it makes - with a check batch of hundreds to
    # thousands of links, that drowns out the worker's own logging.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(scheduler.run(db_path, dashboard_path))


if __name__ == "__main__":
    main()
