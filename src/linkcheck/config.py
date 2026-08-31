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

# How many link-hops deep crawl_site's BFS will follow from a course page before it
# stops expanding the frontier, win or lose - a defensive backstop against unbounded
# fanout from a page structure the curriculum content isn't expected to have, not a
# value expected to actually bind. In practice the reachable graph runs to four
# figures of pages well before ~8 hops (a shared "helpful resources" block linked from
# every course, some courses' day content chained as separate same-site pages rather
# than external links), so this exists purely as a runaway-fanout guard, not a tuning
# knob for graph size.
CRAWL_MAX_DEPTH = 8

# Page size for the site-wide listing sweep (crawler.list_all_pages) - the ceiling
# WordPress's own REST API enforces for `per_page`, not a politeness choice; a larger
# value 400s. The sweep itself (sparse-fielded down to id/slug/link/modified_gmt) is
# still done for the whole site, cheaply - it's what lets crawl_site's BFS skip a full
# body fetch for a reachable page whose modified_gmt hasn't changed, and skip a wasted
# request entirely for a same-site href that isn't a WordPress page at all (PDFs,
# images, blog posts - see extract_internal_links). Only the *body* fetch (and
# everything downstream: parsing, DB sync, reporting) is scoped to the reachable graph.
CRAWL_PAGE_LIST_PER_PAGE = 100

# Retry/backoff for a 429 from the site itself (a fronting CDN/WAF, not necessarily
# WordPress) - observed in practice under a full-site crawl's request volume
# (hundreds of pages at once), never under a handful of course-page fetches. A quick burst of
# 60+ concurrent requests against the live site didn't trigger it, so this is a
# sustained-volume/longer-window threshold, not a per-request or short-burst one -
# backed off with exponential delay rather than tuned to a specific number of
# requests/second, since the actual threshold isn't published and may change.
CRAWL_RATE_LIMIT_MAX_RETRIES = 5
CRAWL_RATE_LIMIT_BASE_DELAY_SECONDS = 5.0  # doubles each retry; used only when the
                                            # response doesn't send a Retry-After header

# --- check phase / reporting ---
# Standardized "never check, never show up" rules. Each rule declares its own SQL
# predicate (a plain host NOT IN for HostBlacklistRule, an exact-or-subdomain NOT
# for HostSuffixBlacklistRule, a correlated EXISTS over page_links.link_text for
# LinkTextBlacklistRule) but shares the same fields/method
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
class HostSuffixBlacklistRule:
    key: str  # slug: namespaces this rule's SQL param names, avoiding collisions
    label: str  # short display name, for the dashboard
    reason: str  # human sentence, for the dashboard
    kind: str  # "host suffix" - display-only, no logic depends on it
    values: frozenset[str]  # base domains; matches the domain itself and any subdomain

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        # Exact-match HostBlacklistRule can't cover a domain whose subdomains are
        # numerous/arbitrary (e.g. archive.org's per-item ia*/dn* content-node
        # shards) - each domain here matches itself plus "%.{domain}" so any
        # subdomain is covered without enumerating hosts that don't exist yet. The
        # leading "." in the LIKE pattern means "myarchive.org"/"archive.org.evil.com"
        # never collide with a real "archive.org" subdomain.
        if not self.values:
            return "", {}
        conditions = []
        params: dict[str, str] = {}
        for i, domain in enumerate(self.values):
            exact_param = f"{self.key}_{i}_exact"
            suffix_param = f"{self.key}_{i}_suffix"
            conditions.append(f"({host_column} = :{exact_param} OR {host_column} LIKE :{suffix_param})")
            params[exact_param] = domain
            params[suffix_param] = f"%.{domain}"
        return f"AND NOT ({' OR '.join(conditions)})", params


# Enclosing punctuation stripped from link_text (both ends) before a
# LinkTextBlacklistRule comparison - covers common human slips like a stray
# "(source" (unmatched paren left inside the anchor) or "source:"/"source." -
# without stripping *interior* characters, so "Resource"/"outsource" etc. can
# never collapse down to "source" by accident.
_CITATION_STRIP_CHARS = "()[]{}.,:;!?\"'"


@dataclass(frozen=True)
class LinkTextBlacklistRule:
    key: str
    label: str
    reason: str
    kind: str  # "link text" - display-only
    values: frozenset[str]  # trimmed/lowercased/punctuation-stripped link_text values to exclude

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        if not self.values:
            return "", {}
        params = {f"{self.key}_{i}": v for i, v in enumerate(self.values)}
        placeholders = ",".join(f":{name}" for name in params)
        alias = f"{self.key}_pl"  # derived from key, not hardcoded, so two link-text
        # rules folded into one combined clause can never alias-collide
        # SQL-escape any embedded single quote (e.g. an apostrophe in the strip set)
        # by doubling it, per SQLite string-literal syntax.
        strip_literal = _CITATION_STRIP_CHARS.replace("'", "''")
        normalized = f"TRIM(TRIM(LOWER({alias}.link_text)), '{strip_literal}')"
        # EXISTS a page_links row NOT matching the blacklisted text, rather than a
        # blanket NOT IN on links.link_text: only excludes when every reference to
        # the link uses this text - a link cited as "source" on one page but a real
        # course link on another must still show up as a problem.
        return (
            f"""AND EXISTS (
                SELECT 1 FROM page_links {alias}
                WHERE {alias}.link_id = {link_id_column}
                  AND ({alias}.link_text IS NULL
                       OR {normalized} NOT IN ({placeholders}))
            )""",
            params,
        )


@dataclass(frozen=True)
class LinkUrlBlacklistRule:
    key: str  # slug: namespaces this rule's SQL param names, avoiding collisions
    label: str  # short display name, for the dashboard
    reason: str  # human sentence, for the dashboard
    kind: str  # "url" - display-only
    values: frozenset[str]  # exact link URLs to exclude

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        if not self.values:
            return "", {}
        params = {f"{self.key}_{i}": v for i, v in enumerate(self.values)}
        placeholders = ",".join(f":{name}" for name in params)
        # Hardcoded "links.url" rather than a host_column-style parameter: every
        # BLACKLIST_RULES query keeps the links table under that literal name (never
        # aliased away - see link_id_column's own default), and "url" alone would be
        # ambiguous against pages.url wherever a query also joins pages.
        return f"AND links.url NOT IN ({placeholders})", params


@dataclass(frozen=True)
class PageBlacklistRule:
    key: str  # slug: namespaces this rule's SQL param names, avoiding collisions
    label: str  # short display name, for the dashboard
    reason: str  # human sentence, for the dashboard
    kind: str  # "page" - display-only, no logic depends on it
    values: frozenset[str]  # same-site page slugs to never crawl

    def sql_clause(
        self, *, host_column: str = "host", link_id_column: str = "links.id"
    ) -> tuple[str, dict[str, str]]:
        # Enforced at crawl time (crawler.crawl_site never visits these slugs, or
        # anything reachable only through them - see blacklisted_page_slugs), not by
        # filtering the check/report queries - once a page is never crawled, its links
        # never get rows to filter in the first place. Included in BLACKLIST_RULES
        # anyway (rather than a separate list) purely so it shows up on the dashboard
        # next to the other "never checked" rules.
        return "", {}


# Keep each rule's `reason` to 1 sentence, 2 at most - it's dashboard-facing prose,
# not a design doc. Implementation nuance (matching quirks, SQL details) belongs in a
# code comment near the relevant dataclass/field instead.
BLACKLIST_RULES: tuple[
    HostBlacklistRule
    | HostSuffixBlacklistRule
    | LinkTextBlacklistRule
    | PageBlacklistRule
    | LinkUrlBlacklistRule,
    ...,
] = (
    PageBlacklistRule(
        key="parent_submitted_pages",
        label="Parent-submitted course pages",
        kind="page",
        values=frozenset({"parent-submitted-courses", "foresensics-parent-submitted"}),
        reason=(
            "User-submitted course content, not maintained curriculum - never "
            "crawled, along with anything reachable only through them."
        ),
    ),
    HostSuffixBlacklistRule(
        key="never_check_archive_org",
        label="Never-checked hosts (archive.org and subdomains)",
        kind="host suffix",
        values=frozenset({"archive.org"}),
        reason=(
            "web.archive.org (Wayback Machine) is chronically slow/timeout-prone; "
            "the rest of archive.org is excluded alongside it since links can land "
            "on any of its subdomains."
        ),
    ),
    HostBlacklistRule(
        key="dead_host_with_alternate",
        label="Dead hosts, alternate link added beside them",
        kind="host",
        values=frozenset({"123teachme.com", "www.123teachme.com", "apstudynotes.org", "www.apstudynotes.org"}),
        reason=(
            "Appears to be permanently gone - course pages have started pairing "
            "these with an explicit \"(alternate link)\" backup instead of fixing "
            "the original, so it's excluded rather than left to flag as broken "
            "forever."
        ),
    ),
    HostBlacklistRule(
        key="login_required_host",
        label="Login-required hosts",
        kind="host",
        values=frozenset({"www.timetoast.com"}),
        reason=(
            "Requires a logged-in session to view a timeline - every check hits an "
            "auth wall regardless of whether the linked timeline still exists, so a "
            "check response here is never meaningful."
        ),
    ),
    HostBlacklistRule(
        key="unreachable_from_checker_host",
        label="Blocked from the checker's network",
        kind="host",
        values=frozenset({"docsouth.unc.edu"}),
        reason=(
            "Confirmed by hand to fail with \"No route to host\" - a network-level "
            "block between the checker's host and UNC's network, not a real outage, "
            "so a check here is never meaningful."
        ),
    ),
    LinkUrlBlacklistRule(
        key="rot_false_positive",
        label="Rot false positives",
        kind="url",
        values=frozenset({"https://adblockplus.org/en/chrome"}),
        reason=(
            "Confirmed by hand to redirect somewhere still useful - the "
            "homepage_redirect heuristic (rot.py) flags it anyway."
        ),
    ),
    LinkTextBlacklistRule(
        key="source_citation",
        label="Source-citation link text",
        kind="link text",
        values=frozenset({"source"}),
        reason=(
            "Anchor text marking a citation/attribution link, not a link students "
            "are meant to click - both sites pair these with an explicit \"do not "
            "click\" disclaimer."
        ),
    ),
)


@dataclass(frozen=True)
class DesignExclusion:
    label: str  # short display name, for the dashboard
    reason: str  # human sentence, for the dashboard


# Exclusions baked into the crawl/check logic itself rather than expressed as a
# BLACKLIST_RULES entry - unlike that list, this one drives nothing; it exists purely
# so the dashboard can document behavior that would otherwise look like a bug ("why
# isn't this obviously-broken link showing up?"). Not derived from code, so keep it in
# sync by hand whenever crawler.py/checker.py extraction logic changes.
DESIGN_EXCLUSIONS: tuple[DesignExclusion, ...] = (
    DesignExclusion(
        label="No visible link text",
        reason=(
            "Image-only/icon anchors, and anchors wrapping only whitespace, are "
            "dropped at crawl time - there's nothing to show a human trying to "
            "locate the link on the live page."
        ),
    ),
    DesignExclusion(
        label="Same-site links",
        reason=(
            "Only links to a different host are tracked at all - same-site PDFs, "
            "answer keys, and other leaf pages are out of scope."
        ),
    ),
    DesignExclusion(
        label="Fragment / mailto / tel / javascript hrefs",
        reason="Same-page anchor jumps and non-HTTP link types are never real link checks.",
    ),
    DesignExclusion(
        label="Orphaned links",
        reason=(
            "A link no longer referenced by any current page (removed or replaced on "
            "a recrawl) drops out of the check queue - kept in the database in case "
            "it reappears, but never rechecked or reported while orphaned."
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


def blacklisted_page_slugs() -> frozenset[str]:
    """Union of every PageBlacklistRule's slugs in BLACKLIST_RULES - crawler.crawl_site
    skips these slugs (and anything reachable only through them) entirely via the BFS
    frontier, rather than via a SQL predicate (see PageBlacklistRule.sql_clause).
    """
    slugs: set[str] = set()
    for rule in BLACKLIST_RULES:
        if isinstance(rule, PageBlacklistRule):
            slugs |= rule.values
    return frozenset(slugs)

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
CHECK_TIMEOUT_SECONDS = 60
CHECK_MAX_REDIRECTS = 10

# Extended "webpage rot" detection (parked domains, soft-404 pages, redirects
# dumped on a bare homepage) beyond the plain 404/410 definition of broken - see
# linkcheck.rot. Off falls back to classifying only on http_status/error_type, with
# no other behavior change: final_url/page_title are still captured and persisted
# either way (see CheckResult) - only the heuristic checks themselves are skipped.
CHECK_ROT_DETECTION = True

# YouTube serves an empty JS shell to any non-browser client (including this
# checker's plain GET) regardless of whether the video is up - a deleted or private
# video still 200s with no title/body signal to catch it on (all 148 YouTube links
# in the validation sweep had empty titles). The official oEmbed endpoint is probed
# instead for youtube.com/watch and youtu.be URLs (see rot.youtube_video_id/
# is_unavailable_video, checker.check_link): 200 means the video exists, 401 means
# embedding is disabled but the video is still watchable on youtube.com itself (NOT
# broken - left `ok`), 403 means the video is private (unwatchable), 404 means it's
# deleted. Off falls back to fetching the original URL directly, with no oEmbed
# probe and no video_unavailable reason ever applying.
CHECK_YOUTUBE_OEMBED = True

# Cap on how much of a 2xx html response body gets read for rot-detection signals
# (title text, a visible-body excerpt) - stopped early once this many bytes are in,
# not sized to capture a whole page. A soft-404/parking lander's telltale phrases
# are always near the top of the document, so there's nothing to gain from reading
# further at the cost of a slower, heavier check on every single link.
CHECK_BODY_SAMPLE_BYTES = 65536

# Every major browser now defaults to trying https:// before a literal http:// request,
# falling back to http only on a connection-level failure (see notes.md). Mirroring that
# means checking http:// links the way a real visitor's browser actually resolves them
# instead of flagging a stale http-only redirect that no one ever sees. Off switches
# check_link back to checking each URL exactly as stored, with no upgrade attempt.
CHECK_HTTPS_UPGRADE = True

# Some servers serve an incomplete or misordered intermediate chain (a valid leaf cert,
# but nothing linking it to a trusted root) - real browsers paper over this by fetching
# the missing intermediate themselves via the cert's Authority Information Access
# extension (AIA chasing) rather than failing the connection, so a link that every
# visitor's browser reaches fine would otherwise get misreported as broken. See
# linkcheck.aia. Off falls back to the plain bad_ssl_cert classification with no retry.
CHECK_AIA_CHASE = True
AIA_CHASE_TIMEOUT_SECONDS = 10
AIA_CHASE_MAX_HOPS = 5  # generous cap on chain length; guards against a pathological
                        # or cyclical AIA reference chain rather than trusting one blindly

# Some servers send a response with a header line that violates RFC 7230's header
# grammar (seen in practice: helpteaching.com appending a stray "https 200 OK: "
# line) - httpx's parser (h11) is strict and aborts the whole request with
# RemoteProtocolError, even though the response is otherwise a normal 200 that every
# real browser renders fine. stdlib http.client's header parsing is far more
# permissive and accepts these, so a bad-header failure gets one retry through it
# before falling back to the httpx error. Off falls back to the plain `other`
# classification with no retry.
CHECK_LENIENT_HTTP_FALLBACK = True

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
BROKEN_RECHECK_DAYS = 1  # recheck interval once a link is confirmed broken/unreachable

# Links confirmed together (e.g. an entire crawl batch) get the same next_check_at, and
# with a fixed interval they'd stay locked in that cohort forever - recreating the same
# spike of due links every cycle instead of it being a one-off. +/-10% desyncs the cohort
# over the first few cycles without meaningfully weakening the recheck-interval guarantee.
RECHECK_JITTER_FRACTION = 0.10

# --- reporting ---
DASHBOARD_HTML_PATH = "public/status.html"  # regenerated at the end of each check-loop cycle
