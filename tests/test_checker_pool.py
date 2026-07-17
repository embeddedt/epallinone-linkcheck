import asyncio
import itertools
from datetime import UTC, datetime

import httpx
import pytest

from linkcheck import db
from linkcheck.checker import AdmissionControl, check_due_links, run_continuous_checks
from linkcheck.crawler import CoursePage, ExtractedLink, sync_course_page

_seed_page_counter = itertools.count(1)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def seed_link(conn, url):
    n = next(_seed_page_counter)
    page = CoursePage(
        wp_id=n, slug=f"page-{n}", canonical_url=f"https://allinonehomeschool.com/page-{n}/",
        title="Page", html="",
    )
    sync_course_page(conn, "homeschool", page, [ExtractedLink(url=url, text="a", day_context=None)])


def _ok_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))


@pytest.mark.asyncio
async def test_check_due_links_checks_all_seeded_links(conn):
    for i in range(5):
        seed_link(conn, f"https://host{i}.example.com/a")

    seen = []
    async with _ok_client() as client:
        checked = await check_due_links(
            conn, client,
            target=5, global_limit=10,
            admission=AdmissionControl(per_domain_limit=3, min_interval_seconds=0, stale_after_seconds=300),
            on_result=lambda link, result, updated: seen.append(link.url),
        )

    assert checked == 5
    assert len(seen) == 5
    rows = conn.execute("SELECT status FROM links").fetchall()
    assert all(row["status"] == "ok" for row in rows)
    # no leftover claims once everything's done
    assert conn.execute("SELECT COUNT(*) AS n FROM domain_claims").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_check_due_links_respects_target(conn):
    for i in range(5):
        seed_link(conn, f"https://host{i}.example.com/a")

    async with _ok_client() as client:
        checked = await check_due_links(
            conn, client,
            target=2, global_limit=10,
            admission=AdmissionControl(per_domain_limit=3, min_interval_seconds=0, stale_after_seconds=300),
        )

    assert checked == 2
    untouched = conn.execute(
        "SELECT COUNT(*) AS n FROM links WHERE last_checked_at IS NULL"
    ).fetchone()["n"]
    assert untouched == 3


@pytest.mark.asyncio
async def test_check_due_links_gives_up_gracefully_when_rate_limited(conn):
    seed_link(conn, "https://ext.example.com/a")
    seed_link(conn, "https://ext.example.com/b")

    async with _ok_client() as client:
        checked = await check_due_links(
            conn, client,
            target=2, global_limit=10,
            admission=AdmissionControl(
                per_domain_limit=5,
                min_interval_seconds=10,  # much longer than this test should ever run
                stale_after_seconds=300,
            ),
            poll_interval=0.05,
        )

    # best-effort: only the first admission for this host succeeds within the run,
    # rather than hanging around waiting out the full rate-limit window
    assert checked == 1


@pytest.mark.asyncio
async def test_run_continuous_checks_processes_more_than_one_refill_round(conn):
    for i in range(6):
        seed_link(conn, f"https://host{i}.example.com/a")

    stop_event = asyncio.Event()
    seen = []
    async with _ok_client() as client:
        task = asyncio.create_task(
            run_continuous_checks(
                conn, client, stop_event,
                global_limit=10,
                admission=AdmissionControl(per_domain_limit=3, min_interval_seconds=0, stale_after_seconds=300),
                refill_size=2,  # smaller than the seeded backlog - forces multiple polls
                poll_interval=5, fast_poll_interval=0.02,
                on_result=lambda link, result, updated: seen.append(link.url),
            )
        )
        for _ in range(100):
            if len(seen) >= 6:
                break
            await asyncio.sleep(0.02)
        stop_event.set()
        await task

    assert len(seen) == 6
    rows = conn.execute("SELECT status FROM links").fetchall()
    assert all(row["status"] == "ok" for row in rows)


class _SlowTransport(httpx.AsyncBaseTransport):
    """Never resolves until told to - lets a test hold a check open in-flight so
    stop_event can be set while it's still running, rather than racing a real sleep.
    """

    def __init__(self, release: asyncio.Event):
        self.release = release

    async def handle_async_request(self, request):
        await self.release.wait()
        return httpx.Response(200)


@pytest.mark.asyncio
async def test_run_continuous_checks_logs_in_flight_count_on_shutdown(conn, caplog):
    seed_link(conn, "https://ext.example.com/a")

    stop_event = asyncio.Event()
    release = asyncio.Event()
    async with httpx.AsyncClient(transport=_SlowTransport(release)) as client:
        task = asyncio.create_task(
            run_continuous_checks(
                conn, client, stop_event,
                global_limit=10,
                admission=AdmissionControl(per_domain_limit=3, min_interval_seconds=0, stale_after_seconds=300),
                refill_size=10, poll_interval=5, fast_poll_interval=0.02,
            )
        )
        # give the poll loop a moment to claim the link and start the in-flight request
        while not conn.execute("SELECT COUNT(*) AS n FROM domain_claims").fetchone()["n"]:
            await asyncio.sleep(0.01)

        with caplog.at_level("INFO", logger="linkcheck.checker"):
            stop_event.set()
            release.set()  # let the held-open request resolve so the task can finish
            await asyncio.wait_for(task, timeout=1)

    assert "Waiting for 1 in-flight check to finish" in caplog.text


@pytest.mark.asyncio
async def test_run_continuous_checks_stops_promptly(conn):
    stop_event = asyncio.Event()
    async with _ok_client() as client:
        task = asyncio.create_task(
            run_continuous_checks(
                conn, client, stop_event,
                global_limit=10,
                admission=AdmissionControl(per_domain_limit=3, min_interval_seconds=0, stale_after_seconds=300),
                refill_size=10, poll_interval=5, fast_poll_interval=0.02,
            )
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        start = datetime.now(UTC)
        await asyncio.wait_for(task, timeout=1)
        assert (datetime.now(UTC) - start).total_seconds() < 1
