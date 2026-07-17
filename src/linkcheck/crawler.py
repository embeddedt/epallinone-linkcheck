"""Course discovery and page crawling."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import sqlite3
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


def _slug_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


async def fetch_course_page(
    client: httpx.AsyncClient, site: Site, course: CourseLink
) -> CoursePage | None:
    """Fetch a course page's rendered body via the WP REST API, by slug.

    The REST API resolves by slug regardless of the page's URL path shape -
    verified against both flat slugs (`/ep-math-1/`) and pages nested under
    the index page itself (`/individual-courses-of-study/intermediate-language-arts/`).
    Returns None if the slug no longer resolves to a page (course removed/renamed).
    """
    slug = _slug_from_url(course.url)
    response = await client.get(
        f"{site.base_url}/wp-json/wp/v2/pages",
        params={"slug": slug},
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
    data = results[0]
    return CoursePage(
        wp_id=data["id"],
        slug=data["slug"],
        canonical_url=data["link"],
        title=html.unescape(data["title"]["rendered"]),
        html=data["content"]["rendered"],
    )


@dataclass(frozen=True)
class ExtractedLink:
    url: str
    text: str
    day_context: str | None


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
    """
    soup = BeautifulSoup(html, "lxml")
    base_host = urlparse(site_base_url).netloc.lower()

    current_day: str | None = None
    seen: dict[str, ExtractedLink] = {}
    for node in soup.descendants:
        if not isinstance(node, Tag):
            continue
        node_id = node.get("id")
        if node_id and DAY_ID_RE.match(node_id):
            current_day = node_id
        if node.name != "a":
            continue
        href = (node.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = _fix_missing_slash(urljoin(page_url, href)).split("#", 1)[0]
        if not absolute or urlparse(absolute).netloc.lower() == base_host:
            continue
        seen.setdefault(
            absolute,
            ExtractedLink(url=absolute, text=node.get_text(strip=True), day_context=current_day),
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
        INSERT INTO pages (site_id, url, slug, title, last_crawled_at)
        VALUES (:site_id, :url, :slug, :title, :now)
        ON CONFLICT(site_id, url) DO UPDATE SET
            slug = excluded.slug,
            title = excluded.title,
            last_crawled_at = excluded.last_crawled_at
        RETURNING id
        """,
        {
            "site_id": site_id,
            "url": page.canonical_url,
            "slug": page.slug,
            "title": page.title,
            "now": now,
        },
    ).fetchone()
    return row["id"]


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
    page<->link associations are all upserted, then any `page_links` row for a link
    that's no longer present on the page is deleted (`DELETE ... NOT IN <current set>`).
    Links themselves are never hard-deleted here, even if a link ends up with zero
    remaining `page_links` rows after this.
    An orphaned link simply stops being selected by the check phase, since
    that query joins through `page_links`.
    """
    site_id = _get_site_id(conn, site_slug)
    now = _now()
    with conn:
        page_id = _upsert_page(conn, site_id, page)

        current_link_ids: list[int] = []
        for link in links:
            link_id = _upsert_link(conn, link.url)
            current_link_ids.append(link_id)
            conn.execute(
                """
                INSERT INTO page_links (page_id, link_id, day_context, link_text, last_seen_at)
                VALUES (:page_id, :link_id, :day_context, :link_text, :now)
                ON CONFLICT(page_id, link_id) DO UPDATE SET
                    day_context = excluded.day_context,
                    link_text = excluded.link_text,
                    last_seen_at = excluded.last_seen_at
                """,
                {
                    "page_id": page_id,
                    "link_id": link_id,
                    "day_context": link.day_context,
                    "link_text": link.text,
                    "now": now,
                },
            )

        if current_link_ids:
            placeholders = ",".join("?" * len(current_link_ids))
            conn.execute(
                f"DELETE FROM page_links WHERE page_id = ? AND link_id NOT IN ({placeholders})",
                (page_id, *current_link_ids),
            )
        else:
            conn.execute("DELETE FROM page_links WHERE page_id = ?", (page_id,))


@dataclass(frozen=True)
class CrawlResult:
    course: CourseLink
    found: bool
    link_count: int


async def crawl_site(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    site: Site,
    *,
    limit: int | None = None,
    concurrency: int = CRAWL_CONCURRENCY,
    request_delay: float = CRAWL_REQUEST_DELAY_SECONDS,
) -> list[CrawlResult]:
    """Discover, fetch, extract, and sync every course page for one site.

    Bounded by a semaphore rather than fetching everything at once - out of
    politeness to the site being crawled, not because our own volume here is large
    (tens of course pages, not the thousands of leaf pages on the full site).
    Calling the synchronous sync_course_page() from these concurrent coroutines needs
    no lock - see the shared-connection note in scheduler.py for why.
    """
    courses = await discover_courses_for_site(client, site)
    if limit is not None:
        courses = courses[:limit]

    semaphore = asyncio.Semaphore(concurrency)

    async def _crawl_one(course: CourseLink) -> CrawlResult:
        async with semaphore:
            page = await fetch_course_page(client, site, course)
            await asyncio.sleep(request_delay)
        if page is None:
            return CrawlResult(course=course, found=False, link_count=0)
        links = extract_links(page.html, page.canonical_url, site.base_url)
        sync_course_page(conn, site.slug, page, links)
        return CrawlResult(course=course, found=True, link_count=len(links))

    return list(await asyncio.gather(*(_crawl_one(course) for course in courses)))
