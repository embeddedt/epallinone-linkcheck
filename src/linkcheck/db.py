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
    _sync_sites(conn)
    conn.commit()


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
