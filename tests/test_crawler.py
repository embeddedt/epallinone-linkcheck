from pathlib import Path

import httpx
import pytest

from linkcheck import crawler, db
from linkcheck.config import CRAWL_RATE_LIMIT_MAX_RETRIES, Site
from linkcheck.crawler import (
    CourseLink,
    CoursePage,
    ExtractedLink,
    _retry_after_seconds,
    crawl_site,
    discover_course_urls,
    fetch_course_page,
    list_all_pages,
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


def test_retry_after_seconds_parses_numeric_value():
    response = httpx.Response(429, headers={"Retry-After": "12"})
    assert _retry_after_seconds(response) == 12.0


def test_retry_after_seconds_parses_http_date():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    target = datetime.now(UTC) + timedelta(seconds=30)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(target, usegmt=True)})
    seconds = _retry_after_seconds(response)
    assert seconds is not None
    assert 20 <= seconds <= 30  # slack for test execution time and second-precision formatting


def test_retry_after_seconds_returns_none_when_absent():
    assert _retry_after_seconds(httpx.Response(429)) is None


def test_retry_after_seconds_returns_none_when_unparseable():
    response = httpx.Response(429, headers={"Retry-After": "not-a-value"})
    assert _retry_after_seconds(response) is None


@pytest.mark.asyncio
async def test_fetch_page_retries_after_429_honoring_retry_after_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[{
            "id": 7, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"}, "content": {"rendered": "<p>body</p>"},
            "modified_gmt": "2023-05-26T19:33:31",
        }])

    async with _fetch_client(handler) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert len(calls) == 2
    assert page is not None
    assert page.title == "Math 1"


@pytest.mark.asyncio
async def test_fetch_page_falls_back_to_backoff_without_retry_after_header(monkeypatch):
    monkeypatch.setattr(crawler, "CRAWL_RATE_LIMIT_BASE_DELAY_SECONDS", 0.01)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)  # no Retry-After header at all
        return httpx.Response(200, json=[{
            "id": 7, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"}, "content": {"rendered": "<p>body</p>"},
            "modified_gmt": "2023-05-26T19:33:31",
        }])

    async with _fetch_client(handler) as client:
        page = await fetch_course_page(client, _SITE, _COURSE)
    assert len(calls) == 2
    assert page is not None


@pytest.mark.asyncio
async def test_fetch_page_gives_up_after_max_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0"})

    async with _fetch_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_course_page(client, _SITE, _COURSE)
    assert len(calls) == CRAWL_RATE_LIMIT_MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_list_all_pages_follows_pagination_via_total_pages_header():
    def handler(request):
        assert request.url.params["_fields"] == "id,slug,link,modified_gmt"
        page_num = int(request.url.params["page"])
        if page_num == 1:
            return httpx.Response(
                200, json=[{"id": 1, "slug": "a"}], headers={"X-WP-TotalPages": "2"}
            )
        assert page_num == 2
        return httpx.Response(200, json=[{"id": 2, "slug": "b"}])

    async with _fetch_client(handler) as client:
        pages = await list_all_pages(client, _SITE)
    assert [p["slug"] for p in pages] == ["a", "b"]


_INDEX_HTML = """
<div class="entry-content">
  <a href="https://allinonehomeschool.com/ep-math-1/">Math 1</a>
</div>
"""


def _crawl_client(*, index_html: str, listing_pages: list[list[dict]], slug_handler=None):
    """Mocks the two request shapes crawl_site makes against /wp-json/wp/v2/pages:
    the paginated whole-site listing sweep (a `page` param, no `slug`) and a per-page
    full fetch (a `slug` param). `listing_pages` is one JSON array per listing page;
    the total-pages header is derived from its length.
    """

    def handler(request):
        if request.url.path == "/individual-courses-of-study/":
            return httpx.Response(200, text=index_html)
        if request.url.path == "/wp-json/wp/v2/pages":
            if "slug" in request.url.params:
                if slug_handler is None:
                    raise AssertionError(f"unexpected full-fetch request: {request.url}")
                return slug_handler(request)
            page_num = int(request.url.params.get("page", "1"))
            body = listing_pages[page_num - 1]
            headers = {"X-WP-TotalPages": str(len(listing_pages))} if page_num == 1 else {}
            return httpx.Response(200, json=body, headers=headers)
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

    listing = [[{
        "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
        "modified_gmt": "2023-05-26T19:33:31",
    }]]

    # No slug_handler: any full-fetch attempt raises inside the mock transport, which
    # is how "full fetch should be skipped for an unchanged page" is enforced here.
    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].found is True
    assert results[0].unchanged is True
    assert results[0].kind == "course"
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

    listing = [[{
        "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
        "modified_gmt": "2023-05-26T19:33:31",
    }]]

    def slug_handler(request):
        # force=True must skip the cheap modified_gmt check and go straight to a full
        # fetch, even though the listing's modified_gmt is unchanged
        return httpx.Response(200, json=[{
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"},
            "content": {"rendered": '<a href="https://ext.example.com/b">b</a>'},
            "modified_gmt": "2023-05-26T19:33:31",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing, slug_handler=slug_handler) as client:
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

    listing = [[{
        "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
        "modified_gmt": "2024-01-01T00:00:00",
    }]]

    def slug_handler(request):
        return httpx.Response(200, json=[{
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"},
            "content": {"rendered": '<a href="https://ext.example.com/b">b</a>'},
            "modified_gmt": "2024-01-01T00:00:00",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing, slug_handler=slug_handler) as client:
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


@pytest.mark.asyncio
async def test_crawl_site_tags_pages_outside_the_course_index_as_other():
    conn = db.connect(":memory:")
    db.init_db(conn)

    listing = [[
        {
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "modified_gmt": "2023-01-01T00:00:00",
        },
        {
            "id": 2, "slug": "odd-and-even", "link": "https://allinonehomeschool.com/odd-and-even/",
            "modified_gmt": "2023-01-01T00:00:00",
        },
    ]]

    def slug_handler(request):
        slug = request.url.params["slug"]
        if slug == "ep-math-1":
            wp_id, title, href = 1, "Math 1", "https://ext.example.com/course-link"
        else:
            wp_id, title, href = 2, "Odd and Even", "https://ext.example.com/other-link"
        return httpx.Response(200, json=[{
            "id": wp_id, "slug": slug, "link": f"https://allinonehomeschool.com/{slug}/",
            "title": {"rendered": title},
            "content": {"rendered": f'<a href="{href}">link</a>'},
            "modified_gmt": "2023-01-01T00:00:00",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    by_slug = {r.slug: r for r in results}
    assert by_slug["ep-math-1"].kind == "course"
    assert by_slug["odd-and-even"].kind == "other"

    rows = {row["slug"]: row for row in conn.execute("SELECT slug, kind, sort_order FROM pages")}
    assert rows["ep-math-1"]["kind"] == "course"
    assert rows["ep-math-1"]["sort_order"] == 0
    assert rows["odd-and-even"]["kind"] == "other"
    assert rows["odd-and-even"]["sort_order"] is None


@pytest.mark.asyncio
async def test_crawl_site_reports_course_not_found_when_missing_from_listing():
    conn = db.connect(":memory:")
    db.init_db(conn)

    # The course index still links to ep-math-1, but it no longer shows up anywhere in
    # the whole-site listing (deleted/renamed) - still worth flagging, same as before
    # crawl_site covered the whole site.
    listing = [[]]

    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].slug == "ep-math-1"
    assert results[0].kind == "course"
    assert results[0].found is False


@pytest.mark.asyncio
async def test_crawl_site_limit_still_prioritizes_course_pages():
    conn = db.connect(":memory:")
    db.init_db(conn)

    # "other" page listed before the course page, as the REST API might return it -
    # --limit=1 must still pick the course page, not this one, for a small manual
    # sanity check to stay useful.
    listing = [[
        {
            "id": 2, "slug": "odd-and-even", "link": "https://allinonehomeschool.com/odd-and-even/",
            "modified_gmt": "2023-01-01T00:00:00",
        },
        {
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "modified_gmt": "2023-01-01T00:00:00",
        },
    ]]

    def slug_handler(request):
        assert request.url.params["slug"] == "ep-math-1"
        return httpx.Response(200, json=[{
            "id": 1, "slug": "ep-math-1", "link": "https://allinonehomeschool.com/ep-math-1/",
            "title": {"rendered": "Math 1"},
            "content": {"rendered": '<a href="https://ext.example.com/a">a</a>'},
            "modified_gmt": "2023-01-01T00:00:00",
        }])

    async with _crawl_client(index_html=_INDEX_HTML, listing_pages=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE, limit=1)

    assert len(results) == 1
    assert results[0].slug == "ep-math-1"
