import itertools
from datetime import UTC, datetime, timedelta

import pytest

from linkcheck import db
from linkcheck.checker import (
    AdmissionControl,
    CheckResult,
    claim_checkable_links,
    get_due_links,
    record_check,
    release_claim,
)
from linkcheck.config import UNCONFIRMED_RETRY_MINUTES
from linkcheck.crawler import CoursePage, ExtractedLink, sync_course_page

_seed_page_counter = itertools.count(1)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def seed_link(conn, url="https://ext.example.com/a"):
    # Each call gets its own page - reusing one page across calls would make
    # sync_course_page's stale-link cleanup delete the *previous* call's page_links
    # association, since each call only lists one link as "currently on the page".
    n = next(_seed_page_counter)
    page = CoursePage(
        wp_id=n, slug=f"page-{n}", canonical_url=f"https://allinonehomeschool.com/page-{n}/",
        title="Math 1", html="",
    )
    sync_course_page(conn, "homeschool", page, [ExtractedLink(url=url, text="a", day_context=None)])
    return conn.execute("SELECT id FROM links WHERE url = ?", (url,)).fetchone()["id"]


def test_get_due_links_returns_newly_synced_link_immediately(conn):
    seed_link(conn)
    due = get_due_links(conn, datetime.now(UTC), batch_size=10)
    assert len(due) == 1
    assert due[0].url == "https://ext.example.com/a"


def test_get_due_links_excludes_links_not_yet_due(conn):
    seed_link(conn)
    future = datetime.now(UTC) + timedelta(days=100)
    conn.execute("UPDATE links SET next_check_at = ?", (future.isoformat(),))
    conn.commit()

    due = get_due_links(conn, datetime.now(UTC), batch_size=10)
    assert due == []


def test_get_due_links_excludes_orphaned_links(conn):
    link_id = seed_link(conn)
    # simulate the link being removed from every page (crawler's stale-cleanup path)
    conn.execute("DELETE FROM page_links WHERE link_id = ?", (link_id,))
    conn.commit()

    due = get_due_links(conn, datetime.now(UTC), batch_size=10)
    assert due == []


def test_get_due_links_respects_batch_size(conn):
    seed_link(conn, url="https://ext.example.com/a")
    seed_link(conn, url="https://ext.example.com/b")
    due = get_due_links(conn, datetime.now(UTC), batch_size=1)
    assert len(due) == 1


def test_never_check_hosts_are_excluded_from_due_and_claim(conn):
    # web.archive.org is in NEVER_CHECK_HOSTS - it's crawled and stored but must never
    # surface as due or be claimed, or it would sit in the queue forever
    seed_link(conn, url="https://web.archive.org/web/123/http://x")
    seed_link(conn, url="https://ext.example.com/a")

    due_urls = {d.url for d in get_due_links(conn, datetime.now(UTC), batch_size=10)}
    assert due_urls == {"https://ext.example.com/a"}

    claimed_urls = {c.url for c in _claim(conn)}
    assert claimed_urls == {"https://ext.example.com/a"}


def test_record_check_writes_history_and_updates_link_state(conn):
    seed_link(conn)
    now = datetime.now(UTC)
    due = get_due_links(conn, now, batch_size=10)
    link = due[0]

    record_check(conn, link, CheckResult(404, None, 42), now)

    row = conn.execute("SELECT * FROM links WHERE id = ?", (link.id,)).fetchone()
    assert row["last_http_status"] == 404
    assert row["consecutive_failures"] == 1
    assert row["status"] == "pending"  # unconfirmed after a single failure

    history = conn.execute("SELECT * FROM link_checks WHERE link_id = ?", (link.id,)).fetchall()
    assert len(history) == 1
    assert history[0]["http_status"] == 404
    assert history[0]["response_time_ms"] == 42


def test_record_check_confirms_broken_after_enough_failures(conn):
    seed_link(conn)
    now = datetime.now(UTC)
    due = get_due_links(conn, now, batch_size=10)
    link = due[0]

    for _ in range(len(UNCONFIRMED_RETRY_MINUTES) + 1):
        record_check(conn, link, CheckResult(404, None, 10), now)
        link = get_due_links(conn, now + timedelta(days=999), batch_size=10)[0]

    row = conn.execute("SELECT * FROM links WHERE id = ?", (link.id,)).fetchone()
    assert row["status"] == "broken"


def test_record_check_recovering_link_resets_to_ok(conn):
    seed_link(conn)
    now = datetime.now(UTC)
    due = get_due_links(conn, now, batch_size=10)
    link = due[0]

    record_check(conn, link, CheckResult(404, None, 10), now)
    link = get_due_links(conn, now + timedelta(days=999), batch_size=10)[0]
    record_check(conn, link, CheckResult(200, None, 10), now)

    row = conn.execute("SELECT * FROM links WHERE id = ?", (link.id,)).fetchone()
    assert row["status"] == "ok"
    assert row["consecutive_failures"] == 0


# --- claim_checkable_links() / release_claim() ---


def _claim(conn, limit=10, per_domain_limit=3, min_interval_seconds=0.0, stale_after_seconds=300):
    return claim_checkable_links(
        conn,
        datetime.now(UTC),
        limit,
        admission=AdmissionControl(
            per_domain_limit=per_domain_limit,
            min_interval_seconds=min_interval_seconds,
            stale_after_seconds=stale_after_seconds,
        ),
    )


def test_claim_checkable_links_claims_a_due_link(conn):
    seed_link(conn)
    claimed = _claim(conn)
    assert len(claimed) == 1
    assert claimed[0].url == "https://ext.example.com/a"

    row = conn.execute("SELECT * FROM domain_claims").fetchone()
    assert row["host"] == "ext.example.com"


def test_claim_checkable_links_anti_join_excludes_already_claimed_link(conn):
    seed_link(conn)
    first = _claim(conn)
    assert len(first) == 1

    second = _claim(conn)  # same link, still "claimed" from the first call
    assert second == []


def test_claim_checkable_links_respects_per_domain_concurrency(conn):
    for i in range(5):
        seed_link(conn, url=f"https://ext.example.com/{i}")

    claimed = _claim(conn, per_domain_limit=2)
    assert len(claimed) == 2  # only 2 of the 5 due links for this host get claimed


def test_claim_checkable_links_diverse_hosts_are_not_limited_by_each_other(conn):
    for i in range(5):
        seed_link(conn, url=f"https://host{i}.example.com/a")

    claimed = _claim(conn, per_domain_limit=1)
    assert len(claimed) == 5  # 5 distinct hosts, 1 each - concurrency cap never engages


def test_claim_checkable_links_release_frees_the_concurrency_slot(conn):
    seed_link(conn, url="https://ext.example.com/a")
    seed_link(conn, url="https://ext.example.com/b")

    first = _claim(conn, per_domain_limit=1)
    assert len(first) == 1

    blocked = _claim(conn, per_domain_limit=1)
    assert blocked == []  # host already at its concurrency limit

    release_claim(conn, first[0].id)
    freed = _claim(conn, per_domain_limit=1)
    # a slot opened up - note the just-released link is itself immediately reclaimable
    # too (its next_check_at only ever changes via record_check, not claim/release),
    # so it legitimately wins the due-order tiebreak again; this only asserts that
    # capacity was freed, not which specific link comes back.
    assert len(freed) == 1


def test_claim_checkable_links_rate_limit_blocks_second_link_same_host(conn):
    seed_link(conn, url="https://ext.example.com/a")
    seed_link(conn, url="https://ext.example.com/b")

    first = _claim(conn, per_domain_limit=5, min_interval_seconds=60)
    assert len(first) == 1

    # release the concurrency slot immediately, but the rate-limit clock still applies
    release_claim(conn, first[0].id)
    still_blocked = _claim(conn, per_domain_limit=5, min_interval_seconds=60)
    assert still_blocked == []


def test_claim_checkable_links_rate_limit_admits_again_after_interval_elapses(conn):
    seed_link(conn, url="https://ext.example.com/a")
    seed_link(conn, url="https://ext.example.com/b")

    now = datetime.now(UTC)
    admission = AdmissionControl(
        per_domain_limit=5, min_interval_seconds=60, stale_after_seconds=300
    )
    first = claim_checkable_links(conn, now, 5, admission=admission)
    assert len(first) == 1
    release_claim(conn, first[0].id)

    later = now + timedelta(seconds=61)
    second = claim_checkable_links(conn, later, 5, admission=admission)
    # enough time has passed to admit another request to this host - which specific
    # link comes back isn't the point here (see the concurrency-release test above)
    assert len(second) == 1


def test_claim_checkable_links_purges_stale_claims(conn):
    link_id = seed_link(conn)
    conn.execute(
        "INSERT INTO domain_claims (host, link_id, claimed_at) VALUES (?, ?, ?)",
        ("ext.example.com", link_id, (datetime.now(UTC) - timedelta(hours=1)).isoformat()),
    )
    conn.commit()

    # a claim attempt purges anything older than stale_after_seconds first
    claimed = _claim(conn, per_domain_limit=1, stale_after_seconds=300)
    assert len(claimed) == 1
    assert claimed[0].url == "https://ext.example.com/a"


def test_claim_checkable_links_respects_limit(conn):
    for i in range(5):
        seed_link(conn, url=f"https://host{i}.example.com/a")

    claimed = _claim(conn, limit=2)
    assert len(claimed) == 2


def test_claim_checkable_links_oversubscribed_host_does_not_starve_others(conn):
    # One host (bible.com, in production) has far more due links - all with earlier
    # next_check_at than everything else - than the per-poll LIMIT. A flat "current
    # claim count < per_domain_limit" check can't tell these rows apart from each
    # other, so it used to let all of them win the ORDER BY next_check_at / LIMIT race
    # and starve other, equally-due hosts out of the candidate set entirely.
    for i in range(20):
        seed_link(conn, url=f"https://bible.example.com/{i}")
    seed_link(conn, url="https://other-a.example.com/a")
    seed_link(conn, url="https://other-b.example.com/a")

    claimed = _claim(conn, limit=5, per_domain_limit=3)

    assert len(claimed) == 5
    hosts = {link.host for link in claimed}
    assert hosts == {"bible.example.com", "other-a.example.com", "other-b.example.com"}
    assert sum(1 for link in claimed if link.host == "bible.example.com") == 3


def test_release_claim_removes_the_row(conn):
    seed_link(conn)
    claimed = _claim(conn)
    assert conn.execute("SELECT COUNT(*) AS n FROM domain_claims").fetchone()["n"] == 1

    release_claim(conn, claimed[0].id)
    assert conn.execute("SELECT COUNT(*) AS n FROM domain_claims").fetchone()["n"] == 0
