from pathlib import Path

import httpx
import pytest

from linkcheck import db
from linkcheck.config import Site
from linkcheck.crawler import (
    CourseLink,
    CoursePage,
    ExtractedLink,
    crawl_site,
    discover_course_urls,
    fetch_course_page,
    fetch_page_modified,
    sync_course_page,
)

FIXTURES = Path(__file__).parent / "fixtures"

_SITE = Site(
    slug="homeschool",
    base_url="https://allinonehomeschool.com",
    course_index_url="https://allinonehomeschool.com/individual-courses-of-study/",
)
_COURSE = CourseLink(url="https://allinonehomeschool.com/ep-math-1", title="Math 1")


def _fetch_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_discover_homeschool_courses():
    html = (FIXTURES / "homeschool_index.html").read_text()
    courses = discover_course_urls(
        html,
        index_url="https://allinonehomeschool.com/individual-courses-of-study/",
        base_url="https://allinonehomeschool.com",
    )
    urls = {c.url for c in courses}

    assert len(courses) == len(urls)  # no duplicate URLs
    assert len(courses) > 50
    assert "https://allinonehomeschool.com/ep-math-1" in urls
    assert "https://allinonehomeschool.com/language-arts-1" in urls

    # same-page anchor jumps (subject menu) must not show up as courses
    assert not any("individual-courses-of-study#" in c.url for c in courses)
    assert "https://allinonehomeschool.com/individual-courses-of-study" not in urls

    # cross-domain links to the sister site must be excluded
    assert not any("allinonehighschool.com" in c.url for c in courses)


def test_discover_highschool_courses():
    html = (FIXTURES / "highschool_index.html").read_text()
    courses = discover_course_urls(
        html,
        index_url="https://allinonehighschool.com/full-curriculum/",
        base_url="https://allinonehighschool.com",
    )
    urls = {c.url for c in courses}

    assert len(courses) == len(urls)
    assert len(courses) > 30
    assert "https://allinonehighschool.com/algebra-1-2023-update" in urls
    assert "https://allinonehighschool.com/calculus" in urls

    # cross-domain links (sister site, Khan Academy, College Board, etc.) excluded
    assert not any("allinonehomeschool.com" in c.url for c in courses)
    assert not any("khanacademy.org" in c.url for c in courses)
    assert not any("collegeboard.org" in c.url for c in courses)


def test_discover_dedupes_repeated_hrefs():
    html = """
    <div class="entry-content">
      <a href="https://example.com/course-a/">Course A</a>
      <a href="https://example.com/course-a/">Course A (again)</a>
    </div>
    """
    courses = discover_course_urls(
        html, index_url="https://example.com/index/", base_url="https://example.com"
    )
    assert len(courses) == 1
    assert courses[0].title == "Course A"


def test_discover_returns_empty_without_content_area():
    html = "<html><body><a href='https://example.com/x/'>x</a></body></html>"
    courses = discover_course_urls(
        html, index_url="https://example.com/index/", base_url="https://example.com"
    )
    assert courses == []


@pytest.mark.asyncio
async def test_fetch_course_page_parses_a_found_page():
    def handler(request):
        return httpx.Response(200, json=[{
            "id": 7, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "EP Math 1 &amp; More"}, "content": {"rendered": "<p>body</p>"},
            "modified_gmt": "2023-05-26T19:33:31",
        }])

    async with _fetch_client(handler) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert page is not None
    assert page.wp_id == 7
    assert page.title == "EP Math 1 & More"  # HTML entities unescaped
    assert page.html == "<p>body</p>"
    assert page.modified_gmt == "2023-05-26T19:33:31"


@pytest.mark.asyncio
async def test_fetch_course_page_returns_none_for_missing_slug():
    async with _fetch_client(lambda request: httpx.Response(200, json=[])) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert page is None


@pytest.mark.asyncio
async def test_fetch_course_page_returns_none_on_wp_error_object():
    # a WP REST error is a JSON *object*, not a list - it must be treated as "not found"
    # rather than crashing on results[0] and taking the crawl loop down
    def handler(request):
        return httpx.Response(200, json={"code": "rest_no_route", "message": "No route."})

    async with _fetch_client(handler) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert page is None


@pytest.mark.asyncio
async def test_fetch_page_modified_returns_value_for_found_page():
    def handler(request):
        assert request.url.params["_fields"] == "id,modified_gmt"
        return httpx.Response(200, json=[{"id": 7, "modified_gmt": "2023-05-26T19:33:31"}])

    async with _fetch_client(handler) as client:
        modified = await fetch_page_modified(client, _SITE, "ep-math-1")
    assert modified == "2023-05-26T19:33:31"


@pytest.mark.asyncio
async def test_fetch_page_modified_returns_none_for_missing_slug():
    async with _fetch_client(lambda request: httpx.Response(200, json=[])) as client:
        modified = await fetch_page_modified(client, _SITE, "ep-math-1")
    assert modified is None


_INDEX_HTML = """
<div class="entry-content">
  <a href="https://allinonehomeschool.com/ep-math-1/">Math 1</a>
</div>
"""


def _crawl_client(*, index_html: str, wp_handler):
    def handler(request):
        if request.url.path == "/individual-courses-of-study/":
            return httpx.Response(200, text=index_html)
        if request.url.path == "/wp-json/wp/v2/pages":
            return wp_handler(request)
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_crawl_site_skips_full_fetch_for_unchanged_page():
    conn = db.connect(":memory:")
    db.init_db(conn)
    seeded = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-05-26T19:33:31",
    )
    sync_course_page(
        conn, "homeschool", seeded,
        [ExtractedLink(url="https://ext.example.com/a", text="a", day_context=None)],
    )

    full_fetch_calls = []

    def wp_handler(request):
        if "_fields" in request.url.params:
            return httpx.Response(200, json=[{"id": 1, "modified_gmt": "2023-05-26T19:33:31"}])
        full_fetch_calls.append(request)
        raise AssertionError("full fetch should be skipped for an unchanged page")

    async with _crawl_client(index_html=_INDEX_HTML, wp_handler=wp_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    assert full_fetch_calls == []
    assert len(results) == 1
    assert results[0].found is True
    assert results[0].unchanged is True
    assert results[0].link_count == 1  # unchanged link_count comes from the existing page_links row


@pytest.mark.asyncio
async def test_crawl_site_force_does_full_fetch_even_when_unchanged():
    conn = db.connect(":memory:")
    db.init_db(conn)
    seeded = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-05-26T19:33:31",
    )
    sync_course_page(
        conn, "homeschool", seeded,
        [ExtractedLink(url="https://ext.example.com/a", text="a", day_context=None)],
    )

    def wp_handler(request):
        # force=True must skip the cheap modified_gmt check (no "_fields" request)
        # and go straight to a full fetch, even though modified_gmt is unchanged
        assert "_fields" not in request.url.params
        return httpx.Response(200, json=[{
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"},
            "content": {"rendered": '<a href="https://ext.example.com/b">b</a>'},
            "modified_gmt": "2023-05-26T19:33:31",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, wp_handler=wp_handler) as client:
        results = await crawl_site(conn, client, _SITE, force=True)

    assert len(results) == 1
    assert results[0].found is True
    assert results[0].unchanged is False
    assert results[0].link_count == 1

    link_urls = {
        r["url"] for r in conn.execute(
            "SELECT links.url FROM links JOIN page_links ON page_links.link_id = links.id"
        )
    }
    assert link_urls == {"https://ext.example.com/b"}  # re-extracted, not just touched

    row = conn.execute("SELECT modified_gmt FROM pages WHERE slug = 'ep-math-1'").fetchone()
    assert row["modified_gmt"] == "2023-05-26T19:33:31"


@pytest.mark.asyncio
async def test_crawl_site_does_full_fetch_when_modified_gmt_changed():
    conn = db.connect(":memory:")
    db.init_db(conn)
    seeded = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-05-26T19:33:31",
    )
    sync_course_page(
        conn, "homeschool", seeded,
        [ExtractedLink(url="https://ext.example.com/a", text="a", day_context=None)],
    )

    def wp_handler(request):
        if "_fields" in request.url.params:
            return httpx.Response(200, json=[{"id": 1, "modified_gmt": "2024-01-01T00:00:00"}])
        return httpx.Response(200, json=[{
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"},
            "content": {"rendered": '<a href="https://ext.example.com/b">b</a>'},
            "modified_gmt": "2024-01-01T00:00:00",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, wp_handler=wp_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].found is True
    assert results[0].unchanged is False
    assert results[0].link_count == 1

    link_urls = {
        r["url"] for r in conn.execute(
            "SELECT links.url FROM links JOIN page_links ON page_links.link_id = links.id"
        )
    }
    assert link_urls == {"https://ext.example.com/b"}  # stale link "a" dropped, new link "b" synced

    row = conn.execute("SELECT modified_gmt FROM pages WHERE slug = 'ep-math-1'").fetchone()
    assert row["modified_gmt"] == "2024-01-01T00:00:00"
