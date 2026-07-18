"""Query layer + CLI text report + static HTML dashboard rendering.

Both output forms share the same queries (get_site_summaries, get_problem_links,
get_watch_links) - the text report and the HTML dashboard are just two renderings of
the same data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, select_autoescape

from linkcheck import checker
from linkcheck.config import never_check_host_clause

NOT_OK_STATUSES = ("broken", "unreachable")


def _named_in(prefix: str, values: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    """A comma-separated named-placeholder list plus its params, for an IN (...) clause -
    keeps every report query on named params so config.never_check_host_clause (also
    named) can splice in without mixing param styles.
    """
    params = {f"{prefix}_{i}": v for i, v in enumerate(values)}
    return ",".join(f":{name}" for name in params), params


_LINK_COLUMNS = """
    id, url, host, status, last_http_status, last_error_type,
    consecutive_failures, last_checked_at, first_seen_at, next_check_at,
    (SELECT sites.slug || ': ' || pages.title
     FROM page_links
     JOIN pages ON pages.id = page_links.page_id
     JOIN sites ON sites.id = pages.site_id
     WHERE page_links.link_id = links.id
     ORDER BY sites.slug, pages.title
     LIMIT 1) AS course_key
"""


@dataclass(frozen=True)
class PageRef:
    site_slug: str
    page_title: str
    page_url: str
    day_context: str | None
    day_label: str | None
    link_text: str | None
    context_before: str | None
    context_after: str | None


@dataclass(frozen=True)
class LinkReportRow:
    url: str
    host: str
    status: str
    last_http_status: int | None
    last_error_type: str | None
    consecutive_failures: int
    last_checked_at: str | None
    first_seen_at: str
    next_check_at: str
    pages: list[PageRef]


@dataclass(frozen=True)
class PageGroupEntry:
    link: LinkReportRow
    day_context: str | None
    day_label: str | None
    link_text: str | None
    context_before: str | None
    context_after: str | None


@dataclass(frozen=True)
class PageGroup:
    site_slug: str
    page_title: str
    page_url: str
    entries: list[PageGroupEntry]


def _group_by_page(links: list[LinkReportRow]) -> list[PageGroup]:
    """Flatten each link's page references into (page, link) pairs and group by page,
    so the dashboard can render one table per course page instead of cramming every
    page a link appears on into a single "Found on" column. A link referenced from
    multiple pages lands in each page's group - same reasoning as get_site_summaries:
    it's a real risk from every page it's linked from.
    """
    groups: dict[tuple[str, str, str], list[PageGroupEntry]] = {}
    for link in links:
        for page in link.pages:
            key = (page.site_slug, page.page_title, page.page_url)
            groups.setdefault(key, []).append(
                PageGroupEntry(
                    link=link,
                    day_context=page.day_context,
                    day_label=page.day_label,
                    link_text=page.link_text,
                    context_before=page.context_before,
                    context_after=page.context_after,
                )
            )
    return [
        PageGroup(site_slug=slug, page_title=title, page_url=url, entries=entries)
        for (slug, title, url), entries in sorted(
            groups.items(), key=lambda item: (-len(item[1]), item[0])
        )
    ]


@dataclass(frozen=True)
class SiteSummary:
    slug: str
    ok: int
    broken: int
    unreachable: int
    pending: int
    watching: int
    total: int


def get_site_summaries(conn: sqlite3.Connection) -> list[SiteSummary]:
    """Per-site link counts by status, plus a separate "watching" count. A link
    referenced from pages on both sites (e.g. a homeschool page linking to a
    highschool course) is counted under each site it's referenced from - it's a real
    404 risk from either page's perspective.
    """
    site_slugs = [row["slug"] for row in conn.execute("SELECT slug FROM sites ORDER BY slug")]

    never_clause, never_params = never_check_host_clause("links.host")
    notok_placeholders, notok_params = _named_in("notok", NOT_OK_STATUSES)

    # One grouped pass over the site<->link join: per-status counts and the separate
    # "watching" count (failing but not yet confirmed) as conditional aggregates, rather
    # than scanning the four-table join twice and pivoting in Python.
    rows = conn.execute(
        f"""
        SELECT sites.slug AS slug,
            COUNT(DISTINCT CASE WHEN links.status = 'ok' THEN links.id END) AS ok,
            COUNT(DISTINCT CASE WHEN links.status = 'broken' THEN links.id END) AS broken,
            COUNT(DISTINCT CASE WHEN links.status = 'unreachable' THEN links.id END) AS unreachable,
            COUNT(DISTINCT CASE WHEN links.status = 'pending' THEN links.id END) AS pending,
            COUNT(DISTINCT CASE WHEN links.consecutive_failures > 0
                                 AND links.status NOT IN ({notok_placeholders})
                                THEN links.id END) AS watching
        FROM sites
        JOIN pages ON pages.site_id = sites.id
        JOIN page_links ON page_links.page_id = pages.id
        JOIN links ON links.id = page_links.link_id
        WHERE 1=1
          {never_clause}
        GROUP BY sites.slug
        """,
        {**notok_params, **never_params},
    ).fetchall()
    by_slug = {row["slug"]: row for row in rows}

    summaries = []
    for slug in site_slugs:
        # A site with no linked pages yet produces no group row - report it as all zeros.
        row = by_slug.get(slug)
        ok, broken, unreachable, pending, watching = (
            (row["ok"], row["broken"], row["unreachable"], row["pending"], row["watching"])
            if row
            else (0, 0, 0, 0, 0)
        )
        summaries.append(
            SiteSummary(
                slug=slug,
                ok=ok,
                broken=broken,
                unreachable=unreachable,
                pending=pending,
                watching=watching,
                total=ok + broken + unreachable + pending,
            )
        )
    return summaries


def _rows_with_pages(conn: sqlite3.Connection, link_rows: list) -> list[LinkReportRow]:
    if not link_rows:
        return []

    link_ids = [row["id"] for row in link_rows]
    placeholders = ",".join("?" * len(link_ids))
    page_rows = conn.execute(
        f"""
        SELECT page_links.link_id AS link_id, page_links.day_context AS day_context,
               page_links.day_label AS day_label,
               page_links.link_text AS link_text,
               page_links.context_before AS context_before,
               page_links.context_after AS context_after,
               pages.title AS page_title, pages.url AS page_url, sites.slug AS site_slug
        FROM page_links
        JOIN pages ON pages.id = page_links.page_id
        JOIN sites ON sites.id = pages.site_id
        WHERE page_links.link_id IN ({placeholders})
        ORDER BY sites.slug, pages.title
        """,
        link_ids,
    ).fetchall()

    pages_by_link: dict[int, list[PageRef]] = {}
    for row in page_rows:
        pages_by_link.setdefault(row["link_id"], []).append(
            PageRef(
                site_slug=row["site_slug"],
                page_title=row["page_title"],
                page_url=row["page_url"],
                day_context=row["day_context"],
                day_label=row["day_label"],
                link_text=row["link_text"],
                context_before=row["context_before"],
                context_after=row["context_after"],
            )
        )

    return [
        LinkReportRow(
            url=row["url"],
            host=row["host"],
            status=row["status"],
            last_http_status=row["last_http_status"],
            last_error_type=row["last_error_type"],
            consecutive_failures=row["consecutive_failures"],
            last_checked_at=row["last_checked_at"],
            first_seen_at=row["first_seen_at"],
            next_check_at=row["next_check_at"],
            pages=pages_by_link.get(row["id"], []),
        )
        for row in link_rows
    ]


@dataclass(frozen=True)
class CheckProgress:
    checked: int
    total: int

    @property
    def pct(self) -> float:
        return 100.0 * self.checked / self.total if self.total else 0.0


def get_check_progress(conn: sqlite3.Connection) -> CheckProgress:
    """How many checkable links have been checked at least once, out of the total.

    Scoped to the same population claim_checkable_links draws from (referenced from
    at least one page, host not in NEVER_CHECK_HOSTS) - an orphaned or never-checked
    link never gets checked again, so counting it would keep the percentage from ever
    reaching 100.
    """
    never_clause, never_params = never_check_host_clause("host")
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN last_checked_at IS NOT NULL THEN 1 ELSE 0 END) AS checked
        FROM links
        WHERE EXISTS (SELECT 1 FROM page_links WHERE page_links.link_id = links.id)
          {never_clause}
        """,
        never_params,
    ).fetchone()
    return CheckProgress(checked=row["checked"] or 0, total=row["total"] or 0)


def _link_rows(
    conn: sqlite3.Connection, where: str, order_by: str, params: dict[str, str]
) -> list[LinkReportRow]:
    """Shared skeleton for the broken and watching link lists: same column set,
    NEVER_CHECK_HOSTS exclusion, and current-page-links requirement, differing only in
    the WHERE predicate and ORDER BY.

    The page-links requirement matters beyond just hiding stale rows: a link that's
    lost every page association is also excluded from the check queue (see
    claim_checkable_links), so without this filter an orphaned link that failed its
    last check before losing its page(s) would sit in the watching/broken lists
    forever with nothing to click through to and no future check that could ever
    confirm or clear it.
    """
    never_clause, never_params = never_check_host_clause("host")
    link_rows = conn.execute(
        f"""
        SELECT {_LINK_COLUMNS} FROM links
        WHERE {where}
          {never_clause}
          AND EXISTS (SELECT 1 FROM page_links WHERE page_links.link_id = links.id)
        ORDER BY {order_by}
        """,
        {**params, **never_params},
    ).fetchall()
    return _rows_with_pages(conn, link_rows)


def get_problem_links(conn: sqlite3.Connection) -> list[LinkReportRow]:
    """Every link currently confirmed broken or unreachable, with the course
    page(s) it appears on."""
    placeholders, params = _named_in("status", NOT_OK_STATUSES)
    return _link_rows(
        conn,
        where=f"status IN ({placeholders})",
        order_by="status, course_key, consecutive_failures DESC, url",
        params=params,
    )


def get_watch_links(conn: sqlite3.Connection) -> list[LinkReportRow]:
    """Links that have failed at least one check but haven't been confirmed
    broken/unreachable yet - mid the confirm-before-flagging retry schedule in
    checker.next_state(). Surfaced separately as a warning: a link here might just
    be a transient blip, or might be about to graduate into get_problem_links() on
    its next failed retry.

    Excludes NEVER_CHECK_HOSTS: a link on one of those hosts that failed once will
    never get the recheck that would confirm or clear it, so it would otherwise sit
    here forever looking like an unresolved transient blip.
    """
    placeholders, params = _named_in("status", NOT_OK_STATUSES)
    return _link_rows(
        conn,
        where=f"consecutive_failures > 0 AND status NOT IN ({placeholders})",
        order_by="course_key, consecutive_failures DESC, url",
        params=params,
    )


def _outcome(link: LinkReportRow) -> str:
    return checker.outcome(link.last_http_status, link.last_error_type)


def _text_fragment(text: str) -> str:
    """Percent-encode text for a Scroll-To-Text-Fragment directive (`#:~:text=...`).
    Hyphens are significant in the directive's own mini-syntax (they separate the
    optional prefix-/suffix- and start,end segments) so, unlike normal URL encoding, a
    literal hyphen must still be escaped even though urllib.parse.quote treats it as
    always-safe.
    """
    return quote(text, safe="").replace("-", "%2D")


MIN_TEXT_FRAGMENT_WORDS = 5  # below this, the directive is too short to trust - see found_on_href


def found_on_href(group: PageGroup, entry: PageGroupEntry) -> str:
    """Build the "Found on" link target for one broken-link row.

    Layers two independent navigation aids into one URL fragment:
    - the day-id anchor (`#dayN`), when the page has one - safe to use bare because
      extract_links only ever records a day_context that's unique on the page (see
      its docstring); a non-unique id would take a browser to the wrong occurrence.
    - a Scroll-To-Text-Fragment directive (`:~:text=...`), when link_text plus its
      prefix-/suffix- context (the prose immediately before/after, captured at crawl
      time) together reach MIN_TEXT_FRAGMENT_WORDS. A bare link_text alone was tried
      before and reverted - a common phrase like "source" repeated across the page
      matched the wrong one. Anchoring to exact adjacent prose fixes that when there's
      enough of it, but a link like "Soviet" sitting in its own bare list item has no
      surrounding prose to anchor to at all - a single common word is exactly the kind
      of directive a browser's matcher can land on the wrong occurrence of, or refuse
      to match, so below the word threshold it's skipped rather than emitted anyway.

    Falls back to the bare page URL (no fragment) when nothing usable is available -
    browsers without Scroll-To-Text-Fragment support just ignore the unrecognized
    fragment regardless.
    """
    fragment = entry.day_context or ""
    word_count = sum(len((text or "").split()) for text in (entry.context_before, entry.link_text, entry.context_after))
    if entry.link_text and word_count >= MIN_TEXT_FRAGMENT_WORDS:
        text_directive = ""
        if entry.context_before:
            text_directive += f"{_text_fragment(entry.context_before)}-,"
        text_directive += _text_fragment(entry.link_text)
        if entry.context_after:
            text_directive += f",-{_text_fragment(entry.context_after)}"
        fragment += f":~:text={text_directive}"
    return f"{group.page_url}#{fragment}" if fragment else group.page_url


def render_text_report(
    summaries: list[SiteSummary],
    problem_links: list[LinkReportRow],
    watch_links: list[LinkReportRow],
) -> str:
    lines = ["Site summary:"]
    for s in summaries:
        lines.append(
            f"  {s.slug:>12}: {s.total:>5} links   "
            f"ok={s.ok} broken={s.broken} unreachable={s.unreachable} "
            f"pending={s.pending} watching={s.watching}"
        )

    lines.append("")
    if not problem_links:
        lines.append("No broken or unreachable links.")
    else:
        lines.append(f"{len(problem_links)} broken/unreachable links:")
        for link in problem_links:
            lines.append(
                f"  [{link.status:>11}] {_outcome(link):>5} (x{link.consecutive_failures}) {link.url}"
            )
            for page in link.pages:
                day = f", {page.day_label or page.day_context}" if page.day_context else ""
                text = f' — "{page.link_text}"' if page.link_text else ""
                lines.append(f"      on {page.site_slug}: {page.page_title!r}{day}{text}")

    if watch_links:
        lines.append("")
        lines.append(
            f"{len(watch_links)} links failing but not yet confirmed "
            f"(watching - could be a transient blip):"
        )
        for link in watch_links:
            lines.append(
                f"  [{'watching':>11}] {_outcome(link):>5} (x{link.consecutive_failures}) "
                f"{link.url}  next check: {link.next_check_at}"
            )
            for page in link.pages:
                day = f", {page.day_label or page.day_context}" if page.day_context else ""
                text = f' — "{page.link_text}"' if page.link_text else ""
                lines.append(f"      on {page.site_slug}: {page.page_title!r}{day}{text}")

    return "\n".join(lines)


_env = Environment(
    loader=PackageLoader("linkcheck", "templates"),
    autoescape=select_autoescape(["html"]),
)
_env.filters["outcome"] = _outcome
_env.globals["found_on_href"] = found_on_href


def render_html_report(
    summaries: list[SiteSummary],
    problem_links: list[LinkReportRow],
    watch_links: list[LinkReportRow],
    generated_at: str,
) -> str:
    template = _env.get_template("status.html.jinja")
    return template.render(
        summaries=summaries,
        problem_links=problem_links,
        problem_groups=_group_by_page(problem_links),
        watch_links=watch_links,
        watch_groups=_group_by_page(watch_links),
        generated_at=generated_at,
    )
