from pathlib import Path

import httpx
import pytest

from linkcheck.config import Site
from linkcheck.crawler import CourseLink, discover_course_urls, fetch_course_page

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
        }])

    async with _fetch_client(handler) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert page is not None
    assert page.wp_id == 7
    assert page.title == "EP Math 1 & More"  # HTML entities unescaped
    assert page.html == "<p>body</p>"


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
