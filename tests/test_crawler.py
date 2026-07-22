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


def test_discover_skips_malformed_href_without_dropping_other_courses():
    html = """
    <div class="entry-content">
      <a href="http://[invalid-ipv6/oops">bad link</a>
      <a href="https://example.com/course-a/">Course A</a>
    </div>
    """
    courses = discover_course_urls(
        html, index_url="https://example.com/index/", base_url="https://example.com"
    )
    assert [c.url for c in courses] == ["https://example.com/course-a"]


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


_INDEX_HTML = """
<div class="entry-content">
  <a href="https://allinonehomeschool.com/ep-math-1/">Math 1</a>
</div>
"""

_TWO_COURSE_INDEX_HTML = """
<div class="entry-content">
  <a href="https://allinonehomeschool.com/ep-math-1/">Math 1</a>
  <a href="https://allinonehomeschool.com/ep-science-1/">Science 1</a>
</div>
"""


def _crawl_client(*, index_html: str, listing: list[dict], slug_handler=None):
    """Mocks the three request shapes crawl_site makes: the course-index page fetch,
    the site-wide listing sweep (a `page` param, no `slug` - the source of truth for
    "does this slug exist as a WP page, and what's its modified_gmt"), and a per-slug
    full fetch (a `slug` param) - only issued for a page the BFS reaches that's new or
    whose modified_gmt in the listing differs from what's already stored.
    """

    def handler(request):
        if request.url.path == "/individual-courses-of-study/":
            return httpx.Response(200, text=index_html)
        if request.url.path == "/wp-json/wp/v2/pages":
            if "slug" in request.url.params:
                if slug_handler is None:
                    raise AssertionError(f"unexpected full-fetch request: {request.url}")
                return slug_handler(request)
            assert request.url.params.get("page", "1") == "1"  # tests keep listings to one page
            return httpx.Response(200, json=listing, headers={"X-WP-TotalPages": "1"})
        raise AssertionError(f"unexpected request: {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _listing_entry(slug: str, modified_gmt: str) -> dict:
    return {
        "id": abs(hash(slug)) % 100_000, "slug": slug,
        "link": f"https://allinonehomeschool.com/{slug}/", "modified_gmt": modified_gmt,
    }


def _page_response(slug: str, *, wp_id: int, title: str, content: str, modified_gmt: str) -> httpx.Response:
    return httpx.Response(200, json=[{
        "id": wp_id, "slug": slug, "link": f"https://allinonehomeschool.com/{slug}/",
        "title": {"rendered": title}, "content": {"rendered": content}, "modified_gmt": modified_gmt,
    }])


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

    listing = [_listing_entry("ep-math-1", "2023-05-26T19:33:31")]  # matches what's stored

    # No slug_handler: any full-fetch attempt raises inside the mock transport, which
    # is how "full fetch should be skipped for an unchanged page" is enforced here.
    async with _crawl_client(index_html=_INDEX_HTML, listing=listing) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].found is True
    assert results[0].unchanged is True
    assert results[0].kind == "course"
    assert results[0].link_count == 1  # unchanged link_count comes from the existing page_links row


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

    listing = [_listing_entry("ep-math-1", "2024-01-01T00:00:00")]  # changed since last crawl

    def slug_handler(request):
        return _page_response(
            "ep-math-1", wp_id=1, title="Math 1",
            content='<a href="https://ext.example.com/b">b</a>',
            modified_gmt="2024-01-01T00:00:00",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
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
async def test_crawl_site_tags_pages_reached_via_internal_links_as_other():
    conn = db.connect(":memory:")
    db.init_db(conn)

    listing = [_listing_entry("ep-math-1", "2023-01-01"), _listing_entry("odd-and-even", "2023-01-01")]

    def slug_handler(request):
        slug = request.url.params["slug"]
        if slug == "ep-math-1":
            return _page_response(
                slug, wp_id=1, title="Math 1",
                content=(
                    '<a href="https://ext.example.com/course-link">ext</a>'
                    '<a href="https://allinonehomeschool.com/odd-and-even/">Odd and Even</a>'
                ),
                modified_gmt="2023-01-01",
            )
        return _page_response(
            slug, wp_id=2, title="Odd and Even",
            content='<a href="https://ext.example.com/other-link">ext</a>', modified_gmt="2023-01-01",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    by_slug = {r.slug: r for r in results}
    # "odd-and-even" was never in the course index - it only got crawled because
    # ep-math-1's body links to it.
    assert by_slug["ep-math-1"].kind == "course"
    assert by_slug["odd-and-even"].kind == "other"

    rows = {row["slug"]: row for row in conn.execute("SELECT slug, kind, sort_order FROM pages")}
    assert rows["ep-math-1"]["kind"] == "course"
    assert rows["ep-math-1"]["sort_order"] == 0
    assert rows["odd-and-even"]["kind"] == "other"
    assert rows["odd-and-even"]["sort_order"] is None


@pytest.mark.asyncio
async def test_crawl_site_follows_internal_links_transitively():
    conn = db.connect(":memory:")
    db.init_db(conn)

    listing = [_listing_entry(s, "2023-01-01") for s in ("ep-math-1", "day-1-worksheet", "deep-reference")]

    def slug_handler(request):
        slug = request.url.params["slug"]
        if slug == "ep-math-1":
            content = '<a href="https://allinonehomeschool.com/day-1-worksheet/">Day 1</a>'
        elif slug == "day-1-worksheet":
            content = '<a href="https://allinonehomeschool.com/deep-reference/">Deep</a>'
        else:
            content = '<a href="https://ext.example.com/leaf">leaf</a>'
        return _page_response(slug, wp_id=hash(slug) % 1000, title=slug, content=content, modified_gmt="2023-01-01")

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    slugs = {r.slug for r in results}
    assert slugs == {"ep-math-1", "day-1-worksheet", "deep-reference"}
    assert {row["slug"] for row in conn.execute("SELECT slug FROM pages")} == slugs


@pytest.mark.asyncio
async def test_crawl_site_does_not_loop_forever_on_a_link_cycle():
    conn = db.connect(":memory:")
    db.init_db(conn)
    fetch_counts: dict[str, int] = {}
    listing = [_listing_entry("ep-math-1", "2023-01-01"), _listing_entry("page-b", "2023-01-01")]

    def slug_handler(request):
        slug = request.url.params["slug"]
        fetch_counts[slug] = fetch_counts.get(slug, 0) + 1
        if slug == "ep-math-1":
            content = '<a href="https://allinonehomeschool.com/page-b/">B</a>'
        else:
            # page-b links back to the course page itself - a cycle
            content = '<a href="https://allinonehomeschool.com/ep-math-1/">back to Math 1</a>'
        return _page_response(slug, wp_id=1, title=slug, content=content, modified_gmt="2023-01-01")

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    assert fetch_counts == {"ep-math-1": 1, "page-b": 1}  # each slug fetched exactly once
    assert {r.slug for r in results} == {"ep-math-1", "page-b"}


@pytest.mark.asyncio
async def test_crawl_site_does_not_report_a_non_page_internal_link_as_not_found():
    # A same-site href discovered mid-graph that isn't in the site-wide listing (a PDF,
    # an image, a blog post) is normal, not broken - it must not surface as a
    # CrawlResult, and - since the listing already said it doesn't exist as a page -
    # must not cost a wasted full-fetch request either, unlike a course-index entry
    # that fails to resolve.
    conn = db.connect(":memory:")
    db.init_db(conn)
    listing = [_listing_entry("ep-math-1", "2023-01-01")]  # worksheet.pdf is not in the listing

    def slug_handler(request):
        assert request.url.params["slug"] == "ep-math-1"
        return _page_response(
            "ep-math-1", wp_id=1, title="Math 1",
            content='<a href="https://allinonehomeschool.com/worksheet.pdf">worksheet</a>',
            modified_gmt="2023-01-01",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].slug == "ep-math-1"
    assert results[0].found is True


@pytest.mark.asyncio
async def test_crawl_site_reports_course_not_found():
    conn = db.connect(":memory:")
    db.init_db(conn)

    # The course index still links to ep-math-1, but it's absent from the site-wide
    # listing entirely (deleted/renamed) - still worth flagging, and resolved with zero
    # full-fetch requests since the listing already has the full set of existing slugs.
    async with _crawl_client(index_html=_INDEX_HTML, listing=[]) as client:
        results = await crawl_site(conn, client, _SITE)

    assert len(results) == 1
    assert results[0].slug == "ep-math-1"
    assert results[0].kind == "course"
    assert results[0].found is False


@pytest.mark.asyncio
async def test_crawl_site_limit_only_seeds_the_first_n_course_pages():
    conn = db.connect(":memory:")
    db.init_db(conn)
    listing = [_listing_entry("ep-math-1", "2023-01-01"), _listing_entry("ep-science-1", "2023-01-01")]

    def slug_handler(request):
        assert request.url.params["slug"] == "ep-math-1"  # ep-science-1 must never be fetched
        return _page_response(
            "ep-math-1", wp_id=1, title="Math 1",
            content='<a href="https://ext.example.com/a">a</a>', modified_gmt="2023-01-01",
        )

    async with _crawl_client(index_html=_TWO_COURSE_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE, limit=1)

    assert len(results) == 1
    assert results[0].slug == "ep-math-1"


@pytest.mark.asyncio
async def test_crawl_site_prunes_page_links_for_pages_no_longer_reachable():
    conn = db.connect(":memory:")
    db.init_db(conn)

    # Seed a previous crawl where ep-math-1 really did link to an "other" page.
    course_page = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(
        conn, "homeschool", course_page, [],
        internal_links=["https://allinonehomeschool.com/odd-and-even"],
        kind="course", sort_order=0,
    )
    other_page = CoursePage(
        wp_id=2, slug="odd-and-even", canonical_url="https://allinonehomeschool.com/odd-and-even/",
        title="Odd and Even", html="<p>y</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(
        conn, "homeschool", other_page,
        [ExtractedLink(url="https://ext.example.com/stale", text="stale", day_context=None)],
        kind="other", sort_order=None,
    )
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 1

    # This cycle, ep-math-1's content changed and no longer links to odd-and-even.
    listing = [_listing_entry("ep-math-1", "2024-06-01")]

    def slug_handler(request):
        slug = request.url.params["slug"]
        if slug == "ep-math-1":
            return _page_response(slug, wp_id=1, title="Math 1", content="<p>no links now</p>", modified_gmt="2024-06-01")
        raise AssertionError(f"odd-and-even should never be re-fetched, got slug={slug!r}")

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        await crawl_site(conn, client, _SITE)

    # odd-and-even's page row survives (not hard-deleted)...
    assert conn.execute("SELECT id FROM pages WHERE slug = 'odd-and-even'").fetchone() is not None
    # ...but its page_links are gone, so it drops out of checks/reports.
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_crawl_site_skips_pruning_when_limited():
    conn = db.connect(":memory:")
    db.init_db(conn)

    other_page = CoursePage(
        wp_id=2, slug="odd-and-even", canonical_url="https://allinonehomeschool.com/odd-and-even/",
        title="Odd and Even", html="<p>y</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(
        conn, "homeschool", other_page,
        [ExtractedLink(url="https://ext.example.com/kept", text="kept", day_context=None)],
        kind="other", sort_order=None,
    )

    listing = [_listing_entry("ep-math-1", "2023-01-01")]

    def slug_handler(request):
        return _page_response(
            "ep-math-1", wp_id=1, title="Math 1", content="<p>no links</p>", modified_gmt="2023-01-01",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        await crawl_site(conn, client, _SITE, limit=1)

    # A --limit crawl never walked odd-and-even's branch of the real graph, so it must
    # not be treated as evidence that odd-and-even is no longer linked.
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_crawl_site_stops_expanding_past_max_depth():
    conn = db.connect(":memory:")
    db.init_db(conn)
    # every page links to the next one, an unbroken chain deeper than max_depth
    listing = [_listing_entry(s, "2023-01-01") for s in ("ep-math-1", "chain-1", "chain-2")]

    def slug_handler(request):
        slug = request.url.params["slug"]
        n = 0 if slug == "ep-math-1" else int(slug.rsplit("-", 1)[-1])
        child = f"chain-{n + 1}"
        content = f'<a href="https://allinonehomeschool.com/{child}/">next</a>'
        return _page_response(slug, wp_id=n, title=slug, content=content, modified_gmt="2023-01-01")

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE, max_depth=2)

    # depth 0: ep-math-1, depth 1: chain-1, depth 2: chain-2 - chain-3 (unlisted, and
    # would be depth 3) is never even looked up
    assert {r.slug for r in results} == {"ep-math-1", "chain-1", "chain-2"}


@pytest.mark.asyncio
async def test_crawl_site_reuses_persisted_internal_links_for_an_unchanged_page():
    # An unchanged page is touched, not fully fetched - the BFS must still be able to
    # keep expanding through it, using the internal-link edges persisted from its last
    # real crawl rather than losing track of what it links to.
    conn = db.connect(":memory:")
    db.init_db(conn)
    course_page = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(
        conn, "homeschool", course_page, [],
        internal_links=["https://allinonehomeschool.com/odd-and-even"],
        kind="course", sort_order=0,
    )

    listing = [_listing_entry("ep-math-1", "2023-01-01"), _listing_entry("odd-and-even", "2023-01-01")]

    def slug_handler(request):
        assert request.url.params["slug"] == "odd-and-even"  # ep-math-1 must not be re-fetched
        return _page_response(
            "odd-and-even", wp_id=2, title="Odd and Even",
            content='<a href="https://ext.example.com/other-link">ext</a>', modified_gmt="2023-01-01",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    by_slug = {r.slug: r for r in results}
    assert by_slug["ep-math-1"].unchanged is True
    assert by_slug["odd-and-even"].kind == "other"
    assert by_slug["odd-and-even"].link_count == 1


@pytest.mark.asyncio
async def test_crawl_site_forces_a_refetch_for_a_page_from_before_internal_link_tracking():
    # Simulates a DB carried over from before page_internal_links/internal_links_synced_at
    # existed: pages and page_links are populated, but no page has ever had its
    # internal-link edges captured. If crawl_site trusted an "unchanged" modified_gmt
    # here the same way it does for a page synced under the current code, it would
    # believe ep-math-1 has zero children (page_internal_links is empty) and prune
    # odd-and-even's still-genuinely-linked page_links as unreachable. It must instead
    # force one real fetch per reachable page until each one's edges are captured.
    conn = db.connect(":memory:")
    db.init_db(conn)
    course_page = CoursePage(
        wp_id=1, slug="ep-math-1", canonical_url="https://allinonehomeschool.com/ep-math-1/",
        title="Math 1", html="<p>x</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(conn, "homeschool", course_page, [], kind="course", sort_order=0)
    other_page = CoursePage(
        wp_id=2, slug="odd-and-even", canonical_url="https://allinonehomeschool.com/odd-and-even/",
        title="Odd and Even", html="<p>y</p>", modified_gmt="2023-01-01",
    )
    sync_course_page(
        conn, "homeschool", other_page,
        [ExtractedLink(url="https://ext.example.com/x", text="x", day_context=None)],
        kind="other", sort_order=None,
    )
    # Roll back to exactly what a pre-migration DB looked like: sync_course_page above
    # already set internal_links_synced_at and (for ep-math-1) would persist an empty
    # edge set - neither existed under the old whole-site-sweep code.
    conn.execute("UPDATE pages SET internal_links_synced_at = NULL")
    conn.execute("DELETE FROM page_internal_links")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 1

    listing = [_listing_entry("ep-math-1", "2023-01-01"), _listing_entry("odd-and-even", "2023-01-01")]
    fetched: list[str] = []

    def slug_handler(request):
        slug = request.url.params["slug"]
        fetched.append(slug)
        content = (
            '<a href="https://allinonehomeschool.com/odd-and-even/">link</a>'
            if slug == "ep-math-1" else '<a href="https://ext.example.com/x">x</a>'
        )
        return _page_response(slug, wp_id=1, title=slug, content=content, modified_gmt="2023-01-01")

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE)

    # Both pages got a real fetch despite the listing reporting the same modified_gmt
    # already stored (so `unchanged` correctly reports True - the content really is
    # unchanged, it just had to be re-fetched to backfill its internal-link edges), and
    # nothing was wrongly pruned.
    assert set(fetched) == {"ep-math-1", "odd-and-even"}
    by_slug = {r.slug: r for r in results}
    assert by_slug["ep-math-1"].unchanged is True
    assert by_slug["odd-and-even"].unchanged is True
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 1
    assert conn.execute(
        "SELECT internal_links_synced_at FROM pages WHERE slug = 'ep-math-1'"
    ).fetchone()["internal_links_synced_at"] is not None

    # A second crawl now uses the fast path - proof the self-heal is one-time, not sticky.
    async with _crawl_client(index_html=_INDEX_HTML, listing=listing) as client:
        results = await crawl_site(conn, client, _SITE)
    assert all(r.unchanged for r in results)


@pytest.mark.asyncio
async def test_crawl_site_force_bypasses_the_touch_path():
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
    listing = [_listing_entry("ep-math-1", "2023-05-26T19:33:31")]  # matches what's stored
    fetched: list[str] = []

    def slug_handler(request):
        fetched.append(request.url.params["slug"])
        # a fixed dedup rule now drops "a" as a duplicate the old extraction kept
        return _page_response(
            "ep-math-1", wp_id=1, title="Math 1", content="<p>no links</p>", modified_gmt="2023-05-26T19:33:31",
        )

    async with _crawl_client(index_html=_INDEX_HTML, listing=listing, slug_handler=slug_handler) as client:
        results = await crawl_site(conn, client, _SITE, force=True)

    assert fetched == ["ep-math-1"]  # force skipped the touch path, went straight to a full fetch
    assert results[0].unchanged is True  # still accurately reported - modified_gmt really didn't change
    assert results[0].link_count == 0  # re-extraction actually ran and picked up the logic change
    assert conn.execute("SELECT COUNT(*) AS n FROM page_links").fetchone()["n"] == 0
