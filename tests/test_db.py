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
