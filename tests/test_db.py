from linkcheck import db
from linkcheck.config import SITES


def test_init_db_creates_tables_and_seeds_sites():
    conn = db.connect(":memory:")
    db.init_db(conn)

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"sites", "pages", "links", "page_links", "link_checks"} <= tables

    rows = conn.execute("SELECT slug, base_url, course_index_url FROM sites").fetchall()
    assert {row["slug"] for row in rows} == {site.slug for site in SITES}


def test_init_db_is_idempotent():
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.init_db(conn)

    count = conn.execute("SELECT COUNT(*) AS n FROM sites").fetchone()["n"]
    assert count == len(SITES)


def test_init_db_migrates_existing_page_links_table_missing_context_columns():
    # simulates a DB created before context_before/context_after existed in schema.sql -
    # CREATE TABLE IF NOT EXISTS alone would silently skip an already-existing table
    conn = db.connect(":memory:")
    conn.executescript("""
        CREATE TABLE page_links (
            page_id INTEGER NOT NULL,
            link_id INTEGER NOT NULL,
            day_context TEXT,
            link_text TEXT,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (page_id, link_id)
        );
    """)
    conn.execute(
        "INSERT INTO page_links (page_id, link_id, day_context, link_text, last_seen_at) "
        "VALUES (1, 1, 'day1', 'source', '2026-01-01T00:00:00')"
    )
    conn.commit()

    db.init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(page_links)")}
    assert {"context_before", "context_after"} <= columns

    row = conn.execute("SELECT * FROM page_links").fetchone()
    assert row["link_text"] == "source"  # pre-existing data survives the migration
    assert row["context_before"] is None


def test_init_db_migrates_existing_pages_table_missing_modified_gmt():
    # simulates a DB created before modified_gmt existed in schema.sql
    conn = db.connect(":memory:")
    conn.executescript("""
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            site_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            slug TEXT NOT NULL,
            title TEXT,
            last_crawled_at TEXT,
            UNIQUE(site_id, url)
        );
    """)
    conn.execute(
        "INSERT INTO pages (site_id, url, slug, title, last_crawled_at) "
        "VALUES (1, 'https://example.com/x/', 'x', 'X', '2026-01-01T00:00:00')"
    )
    conn.commit()

    db.init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(pages)")}
    assert "modified_gmt" in columns

    row = conn.execute("SELECT * FROM pages").fetchone()
    assert row["title"] == "X"  # pre-existing data survives the migration
    assert row["modified_gmt"] is None
