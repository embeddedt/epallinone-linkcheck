"""SQLite connection and schema management."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

from linkcheck.config import SITES


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers (ad-hoc inspection, the report command) proceed concurrently
    # with the checker's writes instead of blocking on the rollback journal's
    # whole-file lock - a no-op on the :memory: DB the test suite uses, since WAL
    # requires a shared on-disk file. NORMAL sync is WAL's standard pairing: still
    # durable across an application crash, just not fsync-per-commit against a
    # power-loss/OS-crash.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if missing and sync the `sites` config into the DB."""
    schema = resources.files("linkcheck").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    _add_column_if_missing(conn, "page_links", "context_before", "TEXT")
    _add_column_if_missing(conn, "page_links", "context_after", "TEXT")
    _add_column_if_missing(conn, "page_links", "day_label", "TEXT")
    _add_column_if_missing(conn, "pages", "modified_gmt", "TEXT")
    _migrate_page_links_occurrences(conn, schema)
    _sync_sites(conn)
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    """`CREATE TABLE IF NOT EXISTS` only helps on a fresh DB - an existing table needs
    an explicit ALTER TABLE to pick up a column added to schema.sql later. Guarded by
    PRAGMA table_info rather than just trying the ALTER and swallowing the "duplicate
    column" error, since that error string isn't a stable/documented sqlite3 contract.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def _migrate_page_links_occurrences(conn: sqlite3.Connection, schema: str) -> None:
    """Early schema had PRIMARY KEY (page_id, link_id) on page_links - one row per
    page/link pair, so a link referenced from more than one day section on the same
    page silently collapsed onto whichever occurrence the crawler happened to write
    last. The current schema gives page_links its own id and keys uniqueness on
    (page_id, link_id, day_context) instead (day_context now NOT NULL, '' standing in
    for "no day"), so each day's occurrence gets its own row. SQLite can't ALTER a
    table's primary key in place, hence the rename-recreate-copy-drop rather than an
    ALTER TABLE.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(page_links)")}
    if "id" in existing:
        return
    conn.execute("ALTER TABLE page_links RENAME TO page_links_old")
    conn.executescript(schema)  # recreates page_links (IF NOT EXISTS) in the new shape
    conn.execute(
        """
        INSERT INTO page_links
            (page_id, link_id, day_context, day_label, link_text,
             context_before, context_after, last_seen_at)
        SELECT page_id, link_id, COALESCE(day_context, ''), day_label,
               link_text, context_before, context_after, last_seen_at
        FROM page_links_old
        """
    )
    conn.execute("DROP TABLE page_links_old")


def _sync_sites(conn: sqlite3.Connection) -> None:
    conn.executemany(
        """
        INSERT INTO sites (slug, base_url, course_index_url)
        VALUES (:slug, :base_url, :course_index_url)
        ON CONFLICT(slug) DO UPDATE SET
            base_url = excluded.base_url,
            course_index_url = excluded.course_index_url
        """,
        [
            {
                "slug": site.slug,
                "base_url": site.base_url,
                "course_index_url": site.course_index_url,
            }
            for site in SITES
        ],
    )
