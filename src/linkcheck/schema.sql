-- linkcheck database schema

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,             -- 'homeschool' | 'highschool'
    base_url TEXT NOT NULL,
    course_index_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    url TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT,
    last_crawled_at TEXT,
    modified_gmt TEXT,                     -- WP REST API's `modified_gmt` as of last crawl;
                                            -- lets a recrawl skip re-parsing an unchanged page
    UNIQUE(site_id, url)
);

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    host TEXT NOT NULL,                    -- hostname, for per-domain throttling/reporting
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    next_check_at TEXT NOT NULL,           -- due immediately on first insert
    last_http_status INTEGER,
    last_error_type TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' -- pending | ok | broken | unreachable
);
CREATE INDEX IF NOT EXISTS idx_links_next_check ON links(next_check_at);
CREATE INDEX IF NOT EXISTS idx_links_host ON links(host);
-- Lets claim_checkable_links (checker.py) fetch a single host's earliest due links as
-- a bounded indexed seek instead of a scan of that host's whole backlog.
CREATE INDEX IF NOT EXISTS idx_links_host_next_check ON links(host, next_check_at);

CREATE TABLE IF NOT EXISTS page_links (
    page_id INTEGER NOT NULL REFERENCES pages(id),
    link_id INTEGER NOT NULL REFERENCES links(id),
    day_context TEXT,                      -- best-effort nearest id="dayN"; nullable
    link_text TEXT,                        -- anchor text, for readable reports
    context_before TEXT,                   -- best-effort prose immediately before the link
    context_after TEXT,                    -- best-effort prose immediately after the link
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (page_id, link_id)
);
CREATE INDEX IF NOT EXISTS idx_page_links_link ON page_links(link_id);

CREATE TABLE IF NOT EXISTS link_checks (
    id INTEGER PRIMARY KEY,
    link_id INTEGER NOT NULL REFERENCES links(id),
    checked_at TEXT NOT NULL,
    http_status INTEGER,
    error_type TEXT,
    response_time_ms INTEGER,
    classified_broken INTEGER NOT NULL     -- classify() output at check time (0/1)
);
CREATE INDEX IF NOT EXISTS idx_link_checks_link ON link_checks(link_id, checked_at);

-- Per-domain admission control for the check phase, enforced via SQL (join/anti-join)
-- rather than in-process semaphores.
--
-- domain_state: one persistent row per host, tracking the last time a check *started*
-- against it - enforces a minimum interval between request starts (rate limiting),
-- independent of how many are concurrently in flight.
CREATE TABLE IF NOT EXISTS domain_state (
    host TEXT PRIMARY KEY,
    last_request_started_at TEXT
);

-- domain_claims: one row per currently in-flight check, inserted on claim, deleted on
-- completion. Enforces per-domain concurrency (COUNT(*) per host) and - via an
-- anti-join against links.id - guarantees a link already being checked is never
-- claimed a second time. A claim older than a generous staleness threshold (see
-- checker.py) is treated as abandoned (crashed process) and purged, rather than
-- relying on a startup reset - which would incorrectly clobber a genuinely active
-- claim if more than one linkcheck process/invocation is running against the same DB.
CREATE TABLE IF NOT EXISTS domain_claims (
    id INTEGER PRIMARY KEY,
    host TEXT NOT NULL,
    link_id INTEGER NOT NULL REFERENCES links(id),
    claimed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_domain_claims_host ON domain_claims(host);
CREATE INDEX IF NOT EXISTS idx_domain_claims_link ON domain_claims(link_id);
