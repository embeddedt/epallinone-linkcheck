import pytest

from linkcheck import db
from linkcheck.crawler import CoursePage, ExtractedLink, sync_course_page


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def make_page(slug="math-1", title="Math 1", wp_id=1, url="https://allinonehomeschool.com/math-1/"):
    return CoursePage(wp_id=wp_id, slug=slug, canonical_url=url, title=title, html="<p></p>")


def test_sync_creates_page_and_links(conn):
    links = [
        ExtractedLink(url="https://ext.example.com/a", text="a", day_context="day1"),
        ExtractedLink(url="https://ext.example.com/b", text="b", day_context="day2"),
    ]
    sync_course_page(conn, "homeschool", make_page(), links)

    page = conn.execute("SELECT * FROM pages").fetchone()
    assert page["url"] == "https://allinonehomeschool.com/math-1/"
    assert page["title"] == "Math 1"
    assert page["last_crawled_at"] is not None

    link_rows = conn.execute("SELECT * FROM links ORDER BY url").fetchall()
    assert [r["url"] for r in link_rows] == ["https://ext.example.com/a", "https://ext.example.com/b"]
    for row in link_rows:
        assert row["status"] == "pending"
        assert row["next_check_at"] == row["first_seen_at"]  # due immediately

    page_link_rows = conn.execute("SELECT * FROM page_links").fetchall()
    assert len(page_link_rows) == 2


def test_sync_does_not_reset_existing_link_scheduling_state(conn):
    links = [ExtractedLink(url="https://ext.example.com/a", text="a", day_context="day1")]
    sync_course_page(conn, "homeschool", make_page(), links)

    # simulate the check phase having already checked this link
    conn.execute(
        "UPDATE links SET status = 'ok', next_check_at = '2099-01-01T00:00:00', "
        "consecutive_failures = 0, last_checked_at = '2026-01-01T00:00:00'"
    )
    conn.commit()

    # recrawl finds the same link again
    sync_course_page(conn, "homeschool", make_page(), links)

    row = conn.execute("SELECT * FROM links WHERE url = 'https://ext.example.com/a'").fetchone()
    assert row["status"] == "ok"
    assert row["next_check_at"] == "2099-01-01T00:00:00"
    assert row["last_checked_at"] == "2026-01-01T00:00:00"


def test_sync_removes_stale_page_link_but_keeps_link_row(conn):
    first_crawl = [
        ExtractedLink(url="https://ext.example.com/a", text="a", day_context="day1"),
        ExtractedLink(url="https://ext.example.com/b", text="b", day_context="day2"),
    ]
    sync_course_page(conn, "homeschool", make_page(), first_crawl)

    # link "b" removed from the page on the next crawl
    second_crawl = [ExtractedLink(url="https://ext.example.com/a", text="a", day_context="day1")]
    sync_course_page(conn, "homeschool", make_page(), second_crawl)

    # link "b" itself is still tracked (not hard-deleted)...
    b = conn.execute("SELECT id FROM links WHERE url = 'https://ext.example.com/b'").fetchone()
    assert b is not None

    # ...but no longer associated with this page
    page_id = conn.execute("SELECT id FROM pages").fetchone()["id"]
    associated_link_ids = {
        r["link_id"]
        for r in conn.execute("SELECT link_id FROM page_links WHERE page_id = ?", (page_id,))
    }
    assert b["id"] not in associated_link_ids


def test_sync_updates_day_context_and_link_text_on_recrawl(conn):
    sync_course_page(
        conn,
        "homeschool",
        make_page(),
        [ExtractedLink(url="https://ext.example.com/a", text="old text", day_context="day1")],
    )
    sync_course_page(
        conn,
        "homeschool",
        make_page(),
        [ExtractedLink(url="https://ext.example.com/a", text="new text", day_context="day5")],
    )

    row = conn.execute(
        """
        SELECT link_text, day_context FROM page_links
        JOIN links ON links.id = page_links.link_id
        WHERE links.url = 'https://ext.example.com/a'
        """
    ).fetchone()
    assert row["link_text"] == "new text"
    assert row["day_context"] == "day5"


def test_sync_shares_link_across_multiple_pages(conn):
    shared = ExtractedLink(url="https://ext.example.com/shared", text="shared", day_context=None)
    sync_course_page(conn, "homeschool", make_page(slug="math-1", wp_id=1, url="https://allinonehomeschool.com/math-1/"), [shared])
    sync_course_page(conn, "homeschool", make_page(slug="math-2", wp_id=2, url="https://allinonehomeschool.com/math-2/"), [shared])

    link_rows = conn.execute("SELECT * FROM links WHERE url = 'https://ext.example.com/shared'").fetchall()
    assert len(link_rows) == 1

    page_link_rows = conn.execute("SELECT * FROM page_links").fetchall()
    assert len(page_link_rows) == 2


def test_sync_removes_all_page_links_when_page_has_no_links_anymore(conn):
    sync_course_page(
        conn, "homeschool", make_page(), [ExtractedLink(url="https://ext.example.com/a", text="a", day_context=None)]
    )
    sync_course_page(conn, "homeschool", make_page(), [])

    page_id = conn.execute("SELECT id FROM pages").fetchone()["id"]
    remaining = conn.execute("SELECT * FROM page_links WHERE page_id = ?", (page_id,)).fetchall()
    assert remaining == []

    # the link row itself still exists
    a = conn.execute("SELECT id FROM links WHERE url = 'https://ext.example.com/a'").fetchone()
    assert a is not None


def test_sync_reappearing_link_is_re_associated_and_keeps_scheduling_state(conn):
    link = ExtractedLink(url="https://ext.example.com/a", text="a", day_context="day1")
    sync_course_page(conn, "homeschool", make_page(), [link])

    # check phase runs and confirms the link broken, advancing its schedule
    conn.execute(
        "UPDATE links SET status = 'broken', next_check_at = '2099-01-01T00:00:00', "
        "consecutive_failures = 3, last_checked_at = '2026-01-01T00:00:00'"
    )
    conn.commit()

    # crawl 2: link no longer on the page - association dropped, link row kept
    sync_course_page(conn, "homeschool", make_page(), [])
    link_id = conn.execute("SELECT id FROM links WHERE url = 'https://ext.example.com/a'").fetchone()["id"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM page_links WHERE link_id = ?", (link_id,)
    ).fetchone()["n"] == 0

    # crawl 3: link reappears on the page
    sync_course_page(conn, "homeschool", make_page(), [link])

    # re-associated so it's checkable again...
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM page_links WHERE link_id = ?", (link_id,)
    ).fetchone()["n"] == 1
    # ...but its prior check-phase state survived the whole round-trip untouched
    row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    assert row["status"] == "broken"
    assert row["next_check_at"] == "2099-01-01T00:00:00"
    assert row["consecutive_failures"] == 3
    assert row["last_checked_at"] == "2026-01-01T00:00:00"


def test_sync_stores_and_updates_surrounding_context(conn):
    sync_course_page(
        conn,
        "homeschool",
        make_page(),
        [ExtractedLink(
            url="https://ext.example.com/a", text="source", day_context="day1",
            context_before="see the", context_after="for more detail",
        )],
    )
    row = conn.execute(
        """
        SELECT context_before, context_after FROM page_links
        JOIN links ON links.id = page_links.link_id
        WHERE links.url = 'https://ext.example.com/a'
        """
    ).fetchone()
    assert row["context_before"] == "see the"
    assert row["context_after"] == "for more detail"

    # recrawl with different surrounding text updates it in place
    sync_course_page(
        conn,
        "homeschool",
        make_page(),
        [ExtractedLink(
            url="https://ext.example.com/a", text="source", day_context="day1",
            context_before="check this", context_after="before continuing",
        )],
    )
    row = conn.execute(
        """
        SELECT context_before, context_after FROM page_links
        JOIN links ON links.id = page_links.link_id
        WHERE links.url = 'https://ext.example.com/a'
        """
    ).fetchone()
    assert row["context_before"] == "check this"
    assert row["context_after"] == "before continuing"


def test_sync_unknown_site_raises(conn):
    with pytest.raises(ValueError, match="Unknown site slug"):
        sync_course_page(conn, "nonexistent", make_page(), [])
