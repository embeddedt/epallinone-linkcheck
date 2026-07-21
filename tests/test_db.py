from linkcheck import db
from linkcheck.config import SITES


def _seed_page_and_link(conn):
    """Real-shaped pages/links tables (matching schema.sql) with one row each (id=1),
    so a page_links row referencing (page_id=1, link_id=1) satisfies the foreign keys
    that INSERT-based migrations (unlike a plain ALTER TABLE ADD COLUMN) actually
    validate - and so db.init_db's own CREATE INDEX statements, which run against
    whatever "links"/"pages" table is already there (CREATE TABLE IF NOT EXISTS is a
    no-op here), find the columns they expect.
    """
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
        CREATE TABLE links (
            id INTEGER PRIMARY KEY,
            url TEXT UNIQUE NOT NULL,
            host TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_checked_at TEXT,
            next_check_at TEXT NOT NULL,
            last_http_status INTEGER,
            last_error_type TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending'
        );
    """)
    conn.execute(
        "INSERT INTO pages (id, site_id, url, slug, title, last_crawled_at) "
        "VALUES (1, 1, 'https://example.com/course/', 'course', 'Course', NULL)"
    )
    conn.execute(
        "INSERT INTO links (id, url, host, first_seen_at, next_check_at) "
        "VALUES (1, 'https://ext.example.com/a', 'ext.example.com', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )


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
    _seed_page_and_link(conn)
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
    assert {"context_before", "context_after", "day_label"} <= columns

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


def test_init_db_migrates_page_links_primary_key_to_support_multiple_occurrences():
    # simulates a DB from before page_links allowed more than one row per (page, link)
    # pair - the old PRIMARY KEY (page_id, link_id) meant a link referenced from more
    # than one day section on the same page silently collapsed onto a single row
    conn = db.connect(":memory:")
    _seed_page_and_link(conn)
    conn.executescript("""
        CREATE TABLE page_links (
            page_id INTEGER NOT NULL,
            link_id INTEGER NOT NULL,
            day_context TEXT,
            day_label TEXT,
            link_text TEXT,
            context_before TEXT,
            context_after TEXT,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (page_id, link_id)
        );
    """)
    conn.execute(
        "INSERT INTO page_links (page_id, link_id, day_context, link_text, last_seen_at) "
        "VALUES (1, 1, 'day12', 'source', '2026-01-01T00:00:00')"
    )
    conn.commit()

    db.init_db(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(page_links)")}
    assert "id" in columns

    row = conn.execute("SELECT * FROM page_links").fetchone()
    assert row["link_text"] == "source"  # pre-existing data survives the migration
    assert row["day_context"] == "day12"

    # the new schema allows a second occurrence of the same link on the same page,
    # distinguished by day - this used to violate the old PRIMARY KEY (page_id, link_id)
    conn.execute(
        "INSERT INTO page_links (page_id, link_id, day_context, link_text, last_seen_at) "
        "VALUES (1, 1, 'day47', 'source', '2026-01-01T00:00:00')"
    )
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM page_links WHERE page_id = 1 AND link_id = 1"
    ).fetchone()["n"]
    assert count == 2


def test_init_db_page_links_migration_is_idempotent():
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.init_db(conn)  # must not error or re-run the migration against the new shape

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(page_links)")}
    assert "id" in columns
