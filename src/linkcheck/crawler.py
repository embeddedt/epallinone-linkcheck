"""Course discovery and page crawling."""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from linkcheck.config import (
    CRAWL_CONCURRENCY,
    CRAWL_PAGE_LIST_PER_PAGE,
    CRAWL_RATE_LIMIT_BASE_DELAY_SECONDS,
    CRAWL_RATE_LIMIT_MAX_RETRIES,
    CRAWL_REQUEST_DELAY_SECONDS,
    USER_AGENT,
    Site,
)

logger = logging.getLogger(__name__)

CONTENT_SELECTOR = ".entry-content"
# "day" is the original/most common marker id; "week" is the same convention used
# throughout the PE/Health, Art, Music, and Computer courses instead of numbering by day.
DAY_ID_RE = re.compile(r"^(?:day|week)\d+$", re.IGNORECASE)
_MISSING_SLASH_RE = re.compile(r"^(https?):/(?!/)", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_DAY_TITLE_RE = re.compile(r"(?:lesson|day|week)\s*\d+\*?", re.IGNORECASE)


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
        raw_href = a["href"].strip()
        try:
            href = _fix_missing_slash(urljoin(index_url, raw_href))
            host = urlparse(href).netloc
        except ValueError:
            # e.g. a stray "[" in the href makes urlsplit think it's a malformed
            # IPv6 host literal and raise - not worth failing the whole crawl over.
            logger.warning("Skipping malformed href %r found on index page %s", raw_href, index_url)
            continue
        href_no_frag = href.split("#")[0].rstrip("/")
        if href_no_frag == index_no_frag:
            continue  # jump link back to the index page itself
        if host != base_host:
            continue  # sister site, or an external resource linked from the index prose
        seen.setdefault(href_no_frag, a.get_text(strip=True))

    return [CourseLink(url=url, title=title) for url, title in seen.items()]


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a `Retry-After` header per RFC 7231 - either delta-seconds or an
    HTTP-date - into seconds to wait. None if the header is absent or unparseable,
    since not every 429 source sends one.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, params: dict | None = None, follow_redirects: bool = False
) -> httpx.Response:
    """GET with bounded retry/backoff on a 429 - seen in practice from the site itself
    (a fronting CDN/WAF, not necessarily WordPress) under the whole-site sweep's
    request volume, never under a handful of course-page fetches. Honors Retry-After
    when the response sends one; falls back to exponential backoff with jitter
    otherwise, since it may not.
    """
    delay = CRAWL_RATE_LIMIT_BASE_DELAY_SECONDS
    for attempt in range(CRAWL_RATE_LIMIT_MAX_RETRIES + 1):
        response = await client.get(
            url, params=params, headers={"User-Agent": USER_AGENT}, follow_redirects=follow_redirects
        )
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == CRAWL_RATE_LIMIT_MAX_RETRIES:
            response.raise_for_status()  # retries exhausted - surface the 429 as before
        wait = _retry_after_seconds(response)
        if wait is None:
            wait = delay + random.uniform(0, delay)  # jitter so concurrent tasks don't retry in lockstep
            delay *= 2
        logger.warning(
            "429 from %s, retrying in %.1fs (attempt %d/%d)",
            url, wait, attempt + 1, CRAWL_RATE_LIMIT_MAX_RETRIES,
        )
        await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # loop above always returns or raises


async def fetch(client: httpx.AsyncClient, url: str) -> str:
    response = await _get_with_retry(client, url, follow_redirects=True)
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


async def _fetch_wp_page(
    client: httpx.AsyncClient, site: Site, slug: str, *, fields: str | None = None
) -> dict | None:
    """Look up a page by slug via the WP REST API. Returns the raw JSON object, or
    None if the slug doesn't resolve to a page (page removed/renamed) or the API
    returned something unexpected.

    The REST API resolves by slug regardless of the page's URL path shape - verified
    against both flat slugs (`/ep-math-1/`) and pages nested under the index page
    itself (`/individual-courses-of-study/intermediate-language-arts/`).
    """
    params = {"slug": slug}
    if fields is not None:
        params["_fields"] = fields
    response = await _get_with_retry(client, f"{site.base_url}/wp-json/wp/v2/pages", params=params)
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


# Sparse fieldset for the whole-site listing sweep (list_all_pages) - enough to tell
# what exists and whether it changed, without paying for the full rendered body of
# every page on the site.
PAGE_LIST_FIELDS = "id,slug,link,modified_gmt"


async def list_all_pages(
    client: httpx.AsyncClient, site: Site, *, per_page: int = CRAWL_PAGE_LIST_PER_PAGE
) -> list[dict]:
    """Enumerate every page on the site - course pages and everything else the site's
    WordPress serves under the 'pages' post type - via the REST API's paginated pages
    collection, sparse-fielded down to id/slug/link/modified_gmt.

    This is what makes a per-page modified_gmt check affordable at thousands-of-pages
    scale: one request per ~100 pages gets existence *and* modified_gmt for the whole
    site, instead of one request per page. `page` past the last one 400s
    (`rest_post_invalid_page_number`) rather than returning an empty list, so the loop
    is bounded by the `X-WP-TotalPages` response header from the first page instead of
    probing until it fails.
    """
    response = await _get_with_retry(
        client,
        f"{site.base_url}/wp-json/wp/v2/pages",
        params={"per_page": per_page, "page": 1, "_fields": PAGE_LIST_FIELDS},
    )
    pages = list(response.json())
    total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
    for page_num in range(2, total_pages + 1):
        response = await _get_with_retry(
            client,
            f"{site.base_url}/wp-json/wp/v2/pages",
            params={"per_page": per_page, "page": page_num, "_fields": PAGE_LIST_FIELDS},
        )
        pages.extend(response.json())
    return pages


async def fetch_page_by_slug(client: httpx.AsyncClient, site: Site, slug: str) -> CoursePage | None:
    """Fetch a page's rendered body via the WP REST API, by slug - course page or not.

    Returns None if the slug no longer resolves to a page (removed/renamed).
    """
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


async def fetch_course_page(
    client: httpx.AsyncClient, site: Site, course: CourseLink
) -> CoursePage | None:
    """Fetch a course page's rendered body via the WP REST API, by slug derived from
    its course-index URL - a thin wrapper over fetch_page_by_slug for callers (the
    discover-courses/crawl-preview manual verification commands) that only have a
    CourseLink, not a bare slug, to start from.
    """
    return await fetch_page_by_slug(client, site, _slug_from_url(course.url))


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

    Day context (the nearest preceding `id="dayN"` or `id="weekN"` - the latter is the
    PE/Health, Art, Music, and Computer courses' convention instead of numbering by day -
    however it's marked up: `<div id="dayN">` on one site, `<strong id="dayN">` on
    another) is captured best-effort purely so reports can say "Math 1, Day 47" instead
    of just "Math 1" - extraction does not depend on it.

    Some course pages reuse the same day id once per week instead of numbering days
    uniquely across the whole page (e.g. "day1" marks the first lesson of every week,
    not just the first lesson overall). `id` values are supposed to be unique, so a
    `#dayN` link only takes a browser to the *first* matching element - on a page like
    that, it would silently jump to the wrong week. day_context is dropped (left None)
    for any day id whose occurrences aren't all one contiguous run (see
    `_marker_run_counts` below), rather than emit an anchor that points somewhere else
    on the page than the link it's meant to locate.

    A day id repeating isn't always that same-id-different-week case, though: some
    pages (e.g. Science Year 4, Bible Geography & Cultures) tag more than one *sibling*
    element within a single lesson with that lesson's own id - a "Materials: ..." callout
    div right after the heading div, or an empty spacer div right before it, each
    carrying the same `id="dayN"` as the heading itself. Jumping to `#dayN` still lands
    a reader in the right lesson there (the first, correct occurrence), since nothing
    else's marker sits between the repeats - unlike the genuine cross-week reuse case,
    where a *different* day's marker appears between one "day1" and the next. Counting
    contiguous runs of the same id (`_marker_run_counts`) rather than raw occurrences
    tells these apart: a same-lesson repeat collapses into one run and stays trusted, a
    cross-week reuse still spans more than one run and gets dropped.

    Links with no visible anchor text (image-only/icon anchors, or anchors
    wrapping only whitespace) are dropped entirely rather than stored with a
    blank link_text - there's nothing to show a human trying to locate the
    link on the live page, and no way to disambiguate one from another if the
    same URL is linked without text in multiple places.

    Deduped per (url, day_context) rather than per url alone - a resource linked from
    more than one day section of the same page (a shared reference site, a recurring
    game) is a distinct occurrence per day, each needing its own fix if it breaks, so
    each one is kept rather than collapsing onto whichever occurrence came first.

    Several courses (Spanish 2, Oceanography, Chemistry/Physics/Earth Science with Lab)
    carry a leftover, spurious `id="day1"` nested *inside* every lesson's real, correctly
    numbered marker - e.g. `<strong id="day2"><strong id="day1">Lesson</strong> 2*</strong>`,
    apparently from copy-pasting Lesson 1's markup as the starting point for every later
    lesson without removing its id. Left alone, that would both flood day_id_counts with
    bogus "day1" occurrences (tripping the duplicate-id guard below for lesson 1 itself)
    and, since descendants are visited outer-then-inner, overwrite current_day with that
    spurious inner id right after the real outer one was set - so `_is_nested_marker`
    ignores any id-bearing tag that sits inside another id-bearing tag, keeping only the
    outer (real) marker. This is separate from - and does not affect - the genuine
    duplicate-id case just below, which is about sibling markers, not nested ones.
    """
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(site_base_url).netloc.lower()

    def _is_nested_marker(node: Tag) -> bool:
        return node.find_parent(id=DAY_ID_RE) is not None

    def _marker_run_counts(marker_nodes: list[Tag]) -> Counter:
        """Count contiguous runs of each id among marker_nodes (already in document
        order), collapsing consecutive repeats of the same id into a single run - see
        the "Materials:"-callout/spacer-div case in extract_links's docstring for why
        that's what actually determines whether `#dayN` is safe to use as an anchor.
        """
        counts: Counter = Counter()
        previous_id: str | None = None
        for node in marker_nodes:
            node_id = node.get("id")
            if node_id != previous_id:
                counts[node_id] += 1
            previous_id = node_id
        return counts

    day_id_counts = _marker_run_counts(
        [node for node in soup.find_all(id=DAY_ID_RE) if not _is_nested_marker(node)]
    )

    current_day: str | None = None
    current_day_label: str | None = None
    seen: dict[tuple[str, str | None], ExtractedLink] = {}
    for node in soup.descendants:
        if not isinstance(node, Tag):
            continue
        node_id = node.get("id")
        if node_id and DAY_ID_RE.match(node_id) and not _is_nested_marker(node):
            # A repeat of the *same* id (the "Materials:"-callout/spacer-div case) is
            # still this same lesson, not a new one - its label is re-derived only on
            # a genuine change of id, so a non-title sibling marker (e.g. a materials
            # callout with no "Lesson N" text of its own) can't clobber the real label
            # the heading marker already set.
            if node_id != current_day:
                current_day_label = _day_label(node)
            current_day = node_id
        if node.name != "a":
            continue
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            absolute = _fix_missing_slash(urljoin(page_url, href)).split("#", 1)[0]
            host = urlparse(absolute).netloc.lower()
        except ValueError:
            # e.g. a stray "[" in the href makes urlsplit think it's a malformed
            # IPv6 host literal and raise - not worth failing the whole crawl over.
            logger.warning("Skipping malformed href %r found on page %s", href, page_url)
            continue
        if not absolute or host == base_host:
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


def _upsert_page(
    conn: sqlite3.Connection, site_id: int, page: CoursePage, *, kind: str, sort_order: int | None
) -> int:
    """kind/sort_order are always overwritten on conflict, not just set on first
    insert - crawl_site recomputes both fresh from the current course-index listing
    every cycle, so this is what lets a page correctly flip between 'course' and
    'other' if the course index itself changes, rather than trusting a stale value
    forever.
    """
    now = _now()
    row = conn.execute(
        """
        INSERT INTO pages (site_id, url, slug, title, last_crawled_at, modified_gmt, kind, sort_order)
        VALUES (:site_id, :url, :slug, :title, :now, :modified_gmt, :kind, :sort_order)
        ON CONFLICT(site_id, url) DO UPDATE SET
            slug = excluded.slug,
            title = excluded.title,
            last_crawled_at = excluded.last_crawled_at,
            modified_gmt = excluded.modified_gmt,
            kind = excluded.kind,
            sort_order = excluded.sort_order
        RETURNING id
        """,
        {
            "site_id": site_id,
            "url": page.canonical_url,
            "slug": page.slug,
            "title": page.title,
            "now": now,
            "modified_gmt": page.modified_gmt,
            "kind": kind,
            "sort_order": sort_order,
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
    conn: sqlite3.Connection,
    site_slug: str,
    page: CoursePage,
    links: list[ExtractedLink],
    *,
    kind: str = "course",
    sort_order: int | None = None,
) -> None:
    """Upsert a crawled page (course or otherwise, see pages.kind) and its links, and
    drop stale associations.

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
        page_id = _upsert_page(conn, site_id, page, kind=kind, sort_order=sort_order)

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
    slug: str
    title: str | None  # None for an 'unchanged' result - not worth a fetch just to report it
    url: str
    kind: str  # 'course' | 'other', see pages.kind
    found: bool
    link_count: int
    unchanged: bool = False  # modified_gmt matched the stored value - full body/parse skipped


def _course_order(courses: list[CourseLink]) -> dict[str, int]:
    """Course slug -> its rank in the course-index listing - both the signal that a
    page is a course (kind='course') rather than 'other', and its report display
    order (pages.sort_order).
    """
    return {_slug_from_url(course.url): i for i, course in enumerate(courses)}


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
    """Discover, fetch, extract, and sync every page on the site in one pass - course
    pages *and* everything else the site's WordPress serves under the 'pages' post
    type (kind='other' - reference/day-content pages a course page links to
    internally, and anything else, checked exactly the same way as a course page).

    Course identity/order comes from the course-index page (discover_courses_for_site);
    which pages currently exist, and whether each one has changed, comes from one
    paginated sweep of the WP REST API's whole pages collection (list_all_pages) - a
    course page is just a page whose slug also happens to appear in that listing, kept
    in sync exactly the same way as any other page. A course-index entry whose slug
    doesn't show up in the listing at all (deleted/renamed course) still surfaces as a
    not-found result, same as before this covered the whole site - list_all_pages
    already tells us the full set of slugs that currently exist, so no extra request
    is needed to notice.

    Bounded by a semaphore rather than fetching everything at once - out of
    politeness to the site being crawled. Calling the synchronous sync_course_page()
    from these concurrent coroutines needs no lock - see the shared-connection note in
    scheduler.py for why.

    force=True skips the modified_gmt cheap-check below so every page gets a full
    fetch/re-extract/sync regardless of whether WordPress reports it as changed - for
    re-applying an extraction-logic change (e.g. a fixed dedup rule) to already-crawled
    pages without waiting for their next real content edit.
    """
    courses = await discover_courses_for_site(client, site)
    course_order = _course_order(courses)

    listing = await list_all_pages(client, site)
    # Courses first, in curriculum order, then everything else in whatever order the
    # REST API returned it (sort is stable) - so a small --limit, used for quick manual
    # sanity checks, still exercises course pages rather than an arbitrary slice of
    # "other" pages.
    listing.sort(key=lambda entry: course_order.get(entry["slug"], len(course_order)))
    listing_slugs = {entry["slug"] for entry in listing}  # full set, before any --limit truncation
    if limit is not None:
        listing = listing[:limit]

    site_id = _get_site_id(conn, site.slug)
    semaphore = asyncio.Semaphore(concurrency)

    async def _crawl_one(entry: dict) -> CrawlResult:
        slug = entry["slug"]
        kind = "course" if slug in course_order else "other"
        sort_order = course_order.get(slug)
        known = _known_page_state(conn, site_id, slug)

        async with semaphore:
            # modified_gmt already came from the listing sweep above, at no extra
            # request cost - most pages don't change day to day, so this skips the
            # full body fetch and link re-extraction for the common case.
            if (
                not force
                and known is not None
                and known["modified_gmt"] is not None
                and entry["modified_gmt"] == known["modified_gmt"]
            ):
                link_count = _touch_page_crawled(conn, known["id"])
                return CrawlResult(
                    slug=slug, title=None, url=entry["link"], kind=kind,
                    found=True, link_count=link_count, unchanged=True,
                )

            page = await fetch_page_by_slug(client, site, slug)
            await asyncio.sleep(request_delay)
        if page is None:
            return CrawlResult(slug=slug, title=None, url=entry["link"], kind=kind, found=False, link_count=0)
        links = extract_links(page.html, page.canonical_url, site.base_url)
        sync_course_page(conn, site.slug, page, links, kind=kind, sort_order=sort_order)
        return CrawlResult(
            slug=slug, title=page.title, url=page.canonical_url, kind=kind,
            found=True, link_count=len(links),
        )

    results = list(await asyncio.gather(*(_crawl_one(entry) for entry in listing)))
    results.extend(
        CrawlResult(slug=slug, title=course.title, url=course.url, kind="course", found=False, link_count=0)
        for course in courses
        if (slug := _slug_from_url(course.url)) not in listing_slugs
    )
    return results
