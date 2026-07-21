"""Course discovery and page crawling."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from linkcheck.config import CRAWL_CONCURRENCY, CRAWL_REQUEST_DELAY_SECONDS, USER_AGENT, Site

logger = logging.getLogger(__name__)

CONTENT_SELECTOR = ".entry-content"
DAY_ID_RE = re.compile(r"^day\d+$", re.IGNORECASE)
_MISSING_SLASH_RE = re.compile(r"^(https?):/(?!/)", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_DAY_TITLE_RE = re.compile(r"(?:lesson|day)\s*\d+\*?", re.IGNORECASE)


def _visible_text(tag: Tag) -> str:
    """Flatten a tag's text the way a browser renders it: concatenate every text node
    as-is, then collapse whitespace runs to a single space.

    `Tag.get_text(strip=True)` strips each individual text node before joining them -
    fine for a human-readable label, but it silently drops/adds whitespace at tag
    boundaries (e.g. "Act V</a>." loses no space, but "the <a>audio</a> here" gains
    one it never had if a separator is passed). Either way the result stops being a
    literal substring of the page's real text, which breaks exact-match consumers
    like the Scroll-To-Text-Fragment context below.
    """
    return _WHITESPACE_RE.sub(" ", tag.get_text()).strip()


def _fix_missing_slash(url: str) -> str:
    """Browsers silently repair `http:/host/path` (single slash after the scheme,
    a typo source pages actually contain) into `http://host/path` - normalize the
    same way so a link a real reader lands on fine isn't reported as broken.
    """
    return _MISSING_SLASH_RE.sub(r"\1://", url)


@dataclass(frozen=True)
class CourseLink:
    url: str
    title: str


def discover_course_urls(html: str, index_url: str, base_url: str) -> list[CourseLink]:
    """Extract course page links from a course index page's body.

    Both sites' index pages are just link lists to courses, but the raw
    `<a href>` set also includes same-page anchor jumps (subject-menu links
    back to the index page itself) and cross-domain links (the two sites
    cross-link to each other's advanced courses, and the index pages embed
    genuine external resources like Khan Academy directly in the prose).
    Restricting to same-domain, non-anchor links filters all of that out in
    one pass.
    """
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one(CONTENT_SELECTOR)
    if content is None:
        return []

    base_host = urlparse(base_url).netloc
    index_no_frag = index_url.split("#")[0].rstrip("/")

    seen: dict[str, str] = {}
    for a in content.find_all("a", href=True):
        href = _fix_missing_slash(urljoin(index_url, a["href"].strip()))
        href_no_frag = href.split("#")[0].rstrip("/")
        if href_no_frag == index_no_frag:
            continue  # jump link back to the index page itself
        if urlparse(href).netloc != base_host:
            continue  # sister site, or an external resource linked from the index prose
        seen.setdefault(href_no_frag, a.get_text(strip=True))

    return [CourseLink(url=url, title=title) for url, title in seen.items()]


async def fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    response.raise_for_status()
    return response.text


async def discover_courses_for_site(client: httpx.AsyncClient, site: Site) -> list[CourseLink]:
    html = await fetch(client, site.course_index_url)
    return discover_course_urls(html, site.course_index_url, site.base_url)


@dataclass(frozen=True)
class CoursePage:
    wp_id: int
    slug: str
    canonical_url: str
    title: str
    html: str
    modified_gmt: str | None = None


def _slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


# Sparse fieldset (WP core's `_fields` param) for the cheap "has this page changed"
# check - a few hundred bytes instead of the full rendered body.
PAGE_MODIFIED_FIELDS = "id,modified_gmt"


async def _fetch_wp_page(
    client: httpx.AsyncClient, site: Site, slug: str, *, fields: str | None = None
) -> dict | None:
    """Look up a page by slug via the WP REST API. Returns the raw JSON object, or
    None if the slug doesn't resolve to a page (course removed/renamed) or the API
    returned something unexpected.

    The REST API resolves by slug regardless of the page's URL path shape - verified
    against both flat slugs (`/ep-math-1/`) and pages nested under the index page
    itself (`/individual-courses-of-study/intermediate-language-arts/`).
    """
    params = {"slug": slug}
    if fields is not None:
        params["_fields"] = fields
    response = await client.get(
        f"{site.base_url}/wp-json/wp/v2/pages",
        params=params,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    results = response.json()
    # A found page is a non-empty JSON array; a missing slug is []. A WP REST *error*
    # is a JSON object ({"code": ..., "message": ...}), which would sail past a bare
    # `if not results` and then blow up on results[0] - treat any non-list shape as
    # "not found" rather than letting one odd response take the crawl loop down.
    if not isinstance(results, list) or not results:
        if results:
            logger.warning("Unexpected WP response for slug %r: %r", slug, results)
        return None
    return results[0]


async def fetch_page_modified(client: httpx.AsyncClient, site: Site, slug: str) -> str | None:
    """Cheap check of a page's current `modified_gmt` via a sparse WP REST fieldset,
    to decide whether a recrawl needs to fetch the full body at all. Verified live
    against WordPress.com-hosted sites: no ETag/Last-Modified header is ever sent on
    this endpoint and conditional GET (If-Modified-Since/If-None-Match) is ignored
    outright, so this is done via WP's own `modified_gmt` field instead of HTTP
    caching semantics. Returns None if the slug no longer resolves to a page - the
    caller falls back to a full fetch either way, which resolves found-vs-not-found
    on its own.
    """
    data = await _fetch_wp_page(client, site, slug, fields=PAGE_MODIFIED_FIELDS)
    return data["modified_gmt"] if data else None


async def fetch_course_page(
    client: httpx.AsyncClient, site: Site, course: CourseLink
) -> CoursePage | None:
    """Fetch a course page's rendered body via the WP REST API, by slug.

    Returns None if the slug no longer resolves to a page (course removed/renamed).
    """
    slug = _slug_from_url(course.url)
    data = await _fetch_wp_page(client, site, slug)
    if data is None:
        return None
    return CoursePage(
        wp_id=data["id"],
        slug=data["slug"],
        canonical_url=data["link"],
        title=html.unescape(data["title"]["rendered"]),
        html=data["content"]["rendered"],
        modified_gmt=data["modified_gmt"],
    )


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    text: str
    day_context: str | None
    context_before: str | None = None
    context_after: str | None = None
    day_label: str | None = None


CONTEXT_CHARS = 60  # how much surrounding prose to keep on each side, best-effort
_CONTEXT_BLOCK_TAGS = ["p", "li", "div", "td", "th", "dd", "dt", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"]


def _truncate_at_word_boundary(text: str, max_chars: int, *, keep_end: bool) -> str:
    """Truncate to at most max_chars without ever cutting a word in half - a
    mid-word cut (e.g. "Read" -> "Re") is still a literal substring of the page's
    text, but it's no longer a *word*, and a Scroll-To-Text-Fragment match that ends
    mid-word is exactly the kind of edge case that trips up browser matchers. Any
    partial word left at the trimmed edge is dropped rather than kept.
    """
    if len(text) <= max_chars:
        return text
    if keep_end:
        cut = text[-max_chars:]
        space = cut.find(" ")
        return cut[space + 1 :] if space != -1 else cut
    cut = text[:max_chars]
    space = cut.rfind(" ")
    return cut[:space] if space != -1 else cut


def _link_context(node: Tag) -> tuple[str | None, str | None]:
    """Best-effort prose immediately before/after a link's anchor text, from its
    nearest block-level ancestor (paragraph, list item, table cell, ...).

    Stored so a human can locate a broken link on the live page by eye or Ctrl-F even
    when day_context is unavailable (or, like link_text alone, ambiguous - "source"
    repeated across a page). Also lets scroll-to-text-fragment be reintroduced later
    with prefix-/suffix- context to disambiguate a repeated anchor phrase, rather than
    matching whichever occurrence happens to come first in the document.
    """
    block = node.find_parent(_CONTEXT_BLOCK_TAGS)
    if block is None:
        return None, None
    block_text = _visible_text(block)
    link_text = _visible_text(node)
    if not link_text:
        return None, None
    idx = block_text.find(link_text)
    if idx == -1:
        return None, None
    before = block_text[:idx].strip()
    after = block_text[idx + len(link_text) :].strip()
    return (
        _truncate_at_word_boundary(before, CONTEXT_CHARS, keep_end=True) or None,
        _truncate_at_word_boundary(after, CONTEXT_CHARS, keep_end=False) or None,
    )


_LABEL_STOP_TAGS = {"ol", "ul"}  # numbered/bulleted lesson body starts here - stop scanning for a title past it
_LABEL_SIBLING_SEARCH_LIMIT = 5  # how many following siblings to check for an empty id marker, see below


def _strong_labels(elements) -> list[str]:
    """Text of each element's title `<strong>` - either the element itself (a `<strong>`
    that's a direct child of the day marker, no wrapper) or a `<strong>` nested inside it
    (the far more common `<p><strong>Lesson N</strong></p>` shape).
    """
    return [
        text
        for el in elements
        if (strong := el if el.name == "strong" else el.find("strong")) and (text := _visible_text(strong))
    ]


def _day_label(node: Tag) -> str | None:
    """Best-effort human-friendly title for a day/lesson marker (e.g. "Lesson 47" -
    or "Day 47" on courses still using that older naming convention), for display
    in reports in place of the raw "day47" id.

    Course pages mark a day several different ways:
    - directly on the id-bearing tag itself (`<strong id="dayN">Lesson N</strong>`)
    - on a wrapping `<div id="dayN">` whose title `<strong>` is itself a direct child
      (`<div id="dayN"><strong>Lesson N</strong> <ol>...`), or nested one level inside
      a direct-child block element (a `<p>` on most pages, a bare `<div>` on at least
      one course)
    - on an empty marker element (`<div id="dayN"></div>`) whose title is actually in
      a handful of *sibling* elements right after it, not a descendant at all

    Some pages put a topic heading (e.g. "Addition") in its own `<strong>` before the
    actual "Lesson N" one, so the first candidate matching "Lesson N"/"Day N" is
    preferred over whichever comes first - scanning stops at the first `<ol>`/`<ul>`,
    since that's where the numbered lesson body (with its own unrelated bold text and,
    on some pages, unrelated numbers - e.g. a link to an external "lesson 1" of some
    other book) begins, and the sibling search additionally stops at the next day
    marker so it never borrows a title from the following day. Trims off any trailing
    boilerplate parenthetical (e.g. "* (Note that an asterisk indicates ...)") by
    keeping only the matched "Lesson N"/"Day N" substring.

    `node`'s own flattened text is only trusted directly when the marker itself has no
    `<ol>`/`<ul>` anywhere inside it (a short marker: just the id-bearing tag itself,
    or a small wrapper holding only the title) - when the marker wraps the day's whole
    body, flattening it would search the exercises' text too, and a stray "lesson 1" or
    "day 3" in there (an external link's own numbering, unrelated to this day) would be
    mistaken for the day's own title.
    """
    own_text = _visible_text(node)
    if own_text and node.find(list(_LABEL_STOP_TAGS)) is None:
        candidates = [own_text]
    else:
        children = []
        for child in node.find_all(recursive=False):
            if child.name in _LABEL_STOP_TAGS:
                break
            children.append(child)
        candidates = _strong_labels(children)

    if not candidates:
        siblings = []
        for sibling in node.find_next_siblings(limit=_LABEL_SIBLING_SEARCH_LIMIT):
            if sibling.name in _LABEL_STOP_TAGS:
                break
            sibling_id = sibling.get("id")
            if sibling_id and DAY_ID_RE.match(sibling_id):
                break
            siblings.append(sibling)
        candidates = _strong_labels(siblings)

    for candidate in candidates:
        match = _DAY_TITLE_RE.search(candidate)
        if match:
            return match.group()
    return candidates[0] if candidates else None


def extract_links(html: str, page_url: str, site_base_url: str) -> list[ExtractedLink]:
    """Pull every external link out of a course page's rendered body.

    "External" means the link's host differs from the site's own host. That
    deliberately includes cross-site links to the sister domain (a real 404
    risk - e.g. a homeschool page linking to a highschool course whose slug
    later changes) while excluding same-site self-links (PDFs, answer keys,
    leaf pages) for now, matching the plan's initial scope of "external links
    going out of the curriculum pages." No day-boundary parsing is needed for
    extraction itself - `content.rendered` already excludes theme chrome, so
    every `<a href>` in it is course content.

    Day context (the nearest preceding `id="dayN"`, however it's marked up -
    `<div id="dayN">` on one site, `<strong id="dayN">` on the other) is
    captured best-effort purely so reports can say "Math 1, Day 47" instead
    of just "Math 1" - extraction does not depend on it.

    Some course pages reuse the same day id once per week instead of numbering days
    uniquely across the whole page (e.g. "day1" marks the first lesson of every week,
    not just the first lesson overall). `id` values are supposed to be unique, so a
    `#dayN` link only takes a browser to the *first* matching element - on a page like
    that, it would silently jump to the wrong week. day_context is dropped (left None)
    for any day id that isn't unique on the page, rather than emit an anchor that
    points somewhere else on the page than the link it's meant to locate.

    Links with no visible anchor text (image-only/icon anchors, or anchors
    wrapping only whitespace) are dropped entirely rather than stored with a
    blank link_text - there's nothing to show a human trying to locate the
    link on the live page, and no way to disambiguate one from another if the
    same URL is linked without text in multiple places.

    Deduped per (url, day_context) rather than per url alone - a resource linked from
    more than one day section of the same page (a shared reference site, a recurring
    game) is a distinct occurrence per day, each needing its own fix if it breaks, so
    each one is kept rather than collapsing onto whichever occurrence came first.
    """
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(site_base_url).netloc.lower()
    day_id_counts = Counter(node.get("id") for node in soup.find_all(id=DAY_ID_RE))

    current_day: str | None = None
    current_day_label: str | None = None
    seen: dict[tuple[str, str | None], ExtractedLink] = {}
    for node in soup.descendants:
        if not isinstance(node, Tag):
            continue
        node_id = node.get("id")
        if node_id and DAY_ID_RE.match(node_id):
            current_day = node_id
            current_day_label = _day_label(node)
        if node.name != "a":
            continue
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _fix_missing_slash(urljoin(page_url, href)).split("#", 1)[0]
        if not absolute or urlparse(absolute).netloc.lower() == base_host:
            continue
        link_text = _visible_text(node)
        if not link_text:
            continue  # image-only/icon anchors with no visible text aren't worth reporting on
        day_context = current_day if current_day and day_id_counts[current_day] == 1 else None
        context_before, context_after = _link_context(node)
        seen.setdefault(
            (absolute, day_context),
            ExtractedLink(
                url=absolute,
                text=link_text,
                day_context=day_context,
                context_before=context_before,
                context_after=context_after,
                day_label=current_day_label if day_context else None,
            ),
        )
    return list(seen.values())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _get_site_id(conn: sqlite3.Connection, site_slug: str) -> int:
    row = conn.execute("SELECT id FROM sites WHERE slug = ?", (site_slug,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown site slug: {site_slug!r}. Run `linkcheck init-db` first.")
    return row["id"]


def _upsert_page(conn: sqlite3.Connection, site_id: int, page: CoursePage) -> int:
    now = _now()
    row = conn.execute(
        """
        INSERT INTO pages (site_id, url, slug, title, last_crawled_at, modified_gmt)
        VALUES (:site_id, :url, :slug, :title, :now, :modified_gmt)
        ON CONFLICT(site_id, url) DO UPDATE SET
            slug = excluded.slug,
            title = excluded.title,
            last_crawled_at = excluded.last_crawled_at,
            modified_gmt = excluded.modified_gmt
        RETURNING id
        """,
        {
            "site_id": site_id,
            "url": page.canonical_url,
            "slug": page.slug,
            "title": page.title,
            "now": now,
            "modified_gmt": page.modified_gmt,
        },
    ).fetchone()
    return row["id"]


def _known_page_state(conn: sqlite3.Connection, site_id: int, slug: str) -> sqlite3.Row | None:
    """Look up a previously crawled page's id and modified_gmt by (site, slug) - the
    stable join key across a recrawl, since a page's URL path can differ from the
    course-index link that discovered it (see fetch_course_page's docstring).
    """
    return conn.execute(
        "SELECT id, modified_gmt FROM pages WHERE site_id = ? AND slug = ?",
        (site_id, slug),
    ).fetchone()


def _touch_page_crawled(conn: sqlite3.Connection, page_id: int) -> int:
    """Record that a page was recrawled and found unchanged, without re-parsing or
    diffing its links. Returns its current link count for the crawl summary.
    """
    now = _now()
    with conn:
        conn.execute("UPDATE pages SET last_crawled_at = ? WHERE id = ?", (now, page_id))
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM page_links WHERE page_id = ?", (page_id,)
        ).fetchone()
    return row["n"]


def _upsert_link(conn: sqlite3.Connection, url: str) -> int:
    """Insert a link if new (due for checking immediately); leave its scheduling state
    untouched if it already exists - the crawl phase must never reset the check phase's
    own state (next_check_at, status, consecutive_failures) just because a link was seen
    again on a recrawl. The `DO UPDATE SET url = url` is a deliberate no-op that changes
    nothing but makes the upsert return the existing row's id via RETURNING (a plain
    DO NOTHING returns no row on conflict), saving a follow-up SELECT.
    """
    now = _now()
    row = conn.execute(
        """
        INSERT INTO links (url, host, first_seen_at, next_check_at)
        VALUES (:url, :host, :now, :now)
        ON CONFLICT(url) DO UPDATE SET url = url
        RETURNING id
        """,
        {"url": url, "host": urlparse(url).netloc.lower(), "now": now},
    ).fetchone()
    return row["id"]


def sync_course_page(
    conn: sqlite3.Connection, site_slug: str, page: CoursePage, links: list[ExtractedLink]
) -> None:
    """Upsert a crawled course page and its links, and drop stale associations.

    Runs as one transaction: the page row, every link found on this crawl, and the
    page<->link associations are all upserted, then any `page_links` row for an
    occurrence (link + day) no longer present on the page is deleted. A link can have
    more than one page_links row per page - one per day section it's referenced from
    (see extract_links) - so staleness is tracked per (link_id, day_context) pair, not
    just per link_id: fixing the link on day 47 but leaving it broken on day 12 of the
    same page must drop only day 47's row, not day 12's too.
    Links themselves are never hard-deleted here, even if a link ends up with zero
    remaining `page_links` rows after this.
    An orphaned link simply stops being selected by the check phase, since
    that query joins through `page_links`.
    """
    site_id = _get_site_id(conn, site_slug)
    now = _now()
    with conn:
        page_id = _upsert_page(conn, site_id, page)

        current_occurrences: set[tuple[int, str]] = set()
        for link in links:
            link_id = _upsert_link(conn, link.url)
            day_context = link.day_context or ""
            current_occurrences.add((link_id, day_context))
            conn.execute(
                """
                INSERT INTO page_links
                    (page_id, link_id, day_context, day_label, link_text, context_before, context_after, last_seen_at)
                VALUES
                    (:page_id, :link_id, :day_context, :day_label, :link_text, :context_before, :context_after, :now)
                ON CONFLICT(page_id, link_id, day_context) DO UPDATE SET
                    day_label = excluded.day_label,
                    link_text = excluded.link_text,
                    context_before = excluded.context_before,
                    context_after = excluded.context_after,
                    last_seen_at = excluded.last_seen_at
                """,
                {
                    "page_id": page_id,
                    "link_id": link_id,
                    "day_context": day_context,
                    "day_label": link.day_label,
                    "link_text": link.text,
                    "context_before": link.context_before,
                    "context_after": link.context_after,
                    "now": now,
                },
            )

        existing = conn.execute(
            "SELECT id, link_id, day_context FROM page_links WHERE page_id = ?",
            (page_id,),
        ).fetchall()
        stale_ids = [
            row["id"]
            for row in existing
            if (row["link_id"], row["day_context"]) not in current_occurrences
        ]
        if stale_ids:
            placeholders = ",".join("?" * len(stale_ids))
            conn.execute(f"DELETE FROM page_links WHERE id IN ({placeholders})", stale_ids)


@dataclass(frozen=True)
class CrawlResult:
    course: CourseLink
    found: bool
    link_count: int
    unchanged: bool = False  # modified_gmt matched the stored value - full body/parse skipped


async def crawl_site(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    site: Site,
    *,
    limit: int | None = None,
    concurrency: int = CRAWL_CONCURRENCY,
    request_delay: float = CRAWL_REQUEST_DELAY_SECONDS,
    force: bool = False,
) -> list[CrawlResult]:
    """Discover, fetch, extract, and sync every course page for one site.

    Bounded by a semaphore rather than fetching everything at once - out of
    politeness to the site being crawled, not because our own volume here is large
    (tens of course pages, not the thousands of leaf pages on the full site).
    Calling the synchronous sync_course_page() from these concurrent coroutines needs
    no lock - see the shared-connection note in scheduler.py for why.

    force=True skips the modified_gmt cheap-check below so every page gets a full
    fetch/re-extract/sync regardless of whether WordPress reports it as changed - for
    re-applying an extraction-logic change (e.g. a fixed dedup rule) to already-crawled
    pages without waiting for their next real content edit.
    """
    courses = await discover_courses_for_site(client, site)
    if limit is not None:
        courses = courses[:limit]

    site_id = _get_site_id(conn, site.slug)
    semaphore = asyncio.Semaphore(concurrency)

    async def _crawl_one(course: CourseLink) -> CrawlResult:
        slug = _slug_from_url(course.url)
        known = _known_page_state(conn, site_id, slug)

        async with semaphore:
            # A page seen before gets a cheap modified_gmt check first - most course
            # pages don't change day to day, so this skips the full body fetch and
            # link re-extraction for the common case (see fetch_page_modified).
            if not force and known is not None and known["modified_gmt"] is not None:
                current_modified = await fetch_page_modified(client, site, slug)
                await asyncio.sleep(request_delay)
                if current_modified is not None and current_modified == known["modified_gmt"]:
                    link_count = _touch_page_crawled(conn, known["id"])
                    return CrawlResult(course=course, found=True, link_count=link_count, unchanged=True)

            page = await fetch_course_page(client, site, course)
            await asyncio.sleep(request_delay)
        if page is None:
            return CrawlResult(course=course, found=False, link_count=0)
        links = extract_links(page.html, page.canonical_url, site.base_url)
        sync_course_page(conn, site.slug, page, links)
        return CrawlResult(course=course, found=True, link_count=len(links))

    return list(await asyncio.gather(*(_crawl_one(course) for course in courses)))
