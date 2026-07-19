"""Site definitions and tuning constants for the crawl and check phases."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    slug: str
    base_url: str
    course_index_url: str


SITES: list[Site] = [
    Site(
        slug="homeschool",
        base_url="https://allinonehomeschool.com",
        course_index_url="https://allinonehomeschool.com/individual-courses-of-study/",
    ),
    Site(
        slug="highschool",
        base_url="https://allinonehighschool.com",
        course_index_url="https://allinonehighschool.com/full-curriculum/",
    ),
]

DEFAULT_DB_PATH = "linkcheck.db"

USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0"
)

# --- crawl phase ---
CRAWL_INTERVAL_MINUTES = 15
CRAWL_CONCURRENCY = 5
CRAWL_REQUEST_DELAY_SECONDS = 0.2
CRAWL_TIMEOUT_SECONDS = 20  # per-request timeout for course-index and page fetches

# --- check phase / reporting ---
# Standardized "never check, never show up" rules. Each rule declares its own SQL
# predicate (a plain host NOT IN for HostBlacklistRule, a correlated EXISTS over
# page_links.link_text for LinkTextBlacklistRule) but shares the same fields/method
# shape, so exclusion_clause() below can fold any number/mix of rules into one
# combined WHERE-clause fragment without needing to know which kind it's holding.
# Splice the result into any query that has a host column and links.id in scope -
# every check-phase and report query does.


@dataclass(frozen=True)
class HostBlacklistRule:
    key: str  # slug: namespaces this rule's SQL param names, avoiding collisions
    label: str  # short display name, for the dashboard
    reason: str  # human sentence, for the dashboard
    kind: str  # "host" - display-only, no logic depends on it
    values: frozenset[str]  # hosts to exclude

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        if not self.values:
            return "", {}
        params = {f"{self.key}_{i}": v for i, v in enumerate(self.values)}
        placeholders = ",".join(f":{name}" for name in params)
        return f"AND {host_column} NOT IN ({placeholders})", params


@dataclass(frozen=True)
class LinkTextBlacklistRule:
    key: str
    label: str
    reason: str
    kind: str  # "link text" - display-only
    values: frozenset[str]  # trimmed/lowercased link_text values to exclude

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        if not self.values:
            return "", {}
        params = {f"{self.key}_{i}": v for i, v in enumerate(self.values)}
        placeholders = ",".join(f":{name}" for name in params)
        alias = f"{self.key}_pl"  # derived from key, not hardcoded, so two link-text
        # rules folded into one combined clause can never alias-collide
        return (
            f"""AND EXISTS (
                SELECT 1 FROM page_links {alias}
                WHERE {alias}.link_id = {link_id_column}
                  AND ({alias}.link_text IS NULL
                       OR TRIM(LOWER({alias}.link_text)) NOT IN ({placeholders}))
            )""",
            params,
        )


BLACKLIST_RULES: tuple[HostBlacklistRule | LinkTextBlacklistRule, ...] = (
    HostBlacklistRule(
        key="never_check_host",
        label="Never-checked hosts",
        kind="host",
        values=frozenset({"web.archive.org"}),
        reason=(
            "Chronically slow/timeout-prone; each attempt burns a full "
            "CHECK_TIMEOUT_SECONDS for no benefit. Still crawled and stored, just "
            "never due for a check."
        ),
    ),
    LinkTextBlacklistRule(
        key="source_citation",
        label="Source-citation link text",
        kind="link text",
        values=frozenset({"source", "source)", "(source)"}),
        reason=(
            "Anchor text marking a citation/attribution link (\"here's where we got "
            "this lesson material from\"), not a link students are meant to click - "
            "both sites pair these with an explicit \"do not click\" disclaimer. "
            "Never checked, and only excluded when every reference to the link uses "
            "this text - a link cited as \"source\" on one page but a real course "
            "link on another must still show up as a problem."
        ),
    ),
)


def exclusion_clause(
    host_column: str = "host", link_id_column: str = "links.id"
) -> tuple[str, dict[str, str]]:
    """Combined SQL fragment (starting with "AND") plus its merged named params, from
    every rule in BLACKLIST_RULES - splice into a query's WHERE clause via an
    f-string and merge the params into that query's params dict. Empty string/dict if
    no rule contributes, so it's always safe to splice in unconditionally.
    """
    fragments: list[str] = []
    params: dict[str, str] = {}
    for rule in BLACKLIST_RULES:
        fragment, rule_params = rule.sql_clause(host_column=host_column, link_id_column=link_id_column)
        if fragment:
            fragments.append(fragment)
            params.update(rule_params)
    return "\n          ".join(fragments), params

# Per-domain concurrency and rate limiting are enforced in SQL against domain_state/
# domain_claims (see schema.sql, checker.claim_checkable_links) - not in-process
# semaphores. CHECK_GLOBAL_CONCURRENCY is just a soft cap on how many checks this
# process keeps outstanding at once (a plain counter, not a shared resource other
# domains contend over).
CHECK_GLOBAL_CONCURRENCY = 50
CHECK_PER_DOMAIN_CONCURRENCY = 3
CHECK_PER_DOMAIN_MIN_INTERVAL_SECONDS = 0.5  # min spacing between request *starts* to
                                              # one host, independent of concurrency -
                                              # caps sustained rate, not just simultaneity
CHECK_TIMEOUT_SECONDS = 15
CHECK_MAX_REDIRECTS = 10

# Every major browser now defaults to trying https:// before a literal http:// request,
# falling back to http only on a connection-level failure (see notes.md). Mirroring that
# means checking http:// links the way a real visitor's browser actually resolves them
# instead of flagging a stale http-only redirect that no one ever sees. Off switches
# check_link back to checking each URL exactly as stored, with no upgrade attempt.
CHECK_HTTPS_UPGRADE = True

# A domain_claims row older than this is treated as an abandoned claim from a crashed
# process and purged rather than trusted - comfortably above CHECK_TIMEOUT_SECONDS so
# a genuinely slow-but-alive check is never mistaken for one.
CHECK_STALE_CLAIM_SECONDS = 300

# How many due links get pulled/claimed from the DB per poll, and how the poll is
# paced: while there's active work (anything claimed or in flight) the feeder fast-polls
# so completions get topped up promptly; only when fully idle does it back off to the
# slow interval. See checker.run_continuous_checks for the exact predicate.
CHECK_BATCH_SIZE = 200
CHECK_LOOP_INTERVAL_SECONDS = 300  # idle poll interval; also the reporting/dashboard cadence
CHECK_FEEDER_FAST_POLL_SECONDS = 1.0  # poll interval while work is in progress
CHECK_ONESHOT_POLL_SECONDS = 0.2  # tight poll for the one-shot `linkcheck check` drain loop

# Heartbeat cadence for the "X/Y links checked (Z%)" progress line - deliberately
# separate from CHECK_LOOP_INTERVAL_SECONDS above, which is tuned for batch refill/
# dashboard freshness, not for "is this thing still alive" visibility during a long
# first-run backlog drain.
CHECK_PROGRESS_LOG_SECONDS = 30

# Retry schedule for a failing link before it's confirmed broken/unreachable - one
# transient blip shouldn't flip a link's status. Length of this tuple implicitly sets
# the confirm threshold: N unconfirmed retries, then the (N+1)th consecutive failure
# confirms it.
UNCONFIRMED_RETRY_MINUTES = (60, 24 * 60)  # 1 hour after the 1st failure, 1 day after the 2nd

HEALTHY_RECHECK_DAYS = 7  # recheck interval once a link is confirmed ok
BROKEN_RECHECK_DAYS = 7  # recheck interval once a link is confirmed broken/unreachable

# Links confirmed together (e.g. an entire crawl batch) get the same next_check_at, and
# with a fixed interval they'd stay locked in that cohort forever - recreating the same
# spike of due links every cycle instead of it being a one-off. +/-10% desyncs the cohort
# over the first few cycles without meaningfully weakening the recheck-interval guarantee.
RECHECK_JITTER_FRACTION = 0.10

# --- reporting ---
DASHBOARD_HTML_PATH = "public/status.html"  # regenerated at the end of each check-loop cycle
