import json
from pathlib import Path

from linkcheck.crawler import extract_links

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_extract_links_div_variant_ep_math_1():
    page = _load("homeschool_ep-math-1.json")
    links = extract_links(
        page["content_html"],
        page_url=page["link"],
        site_base_url="https://allinonehomeschool.com",
    )
    urls = {link.url for link in links}

    assert len(links) == len(urls)  # deduped within the page
    assert len(links) > 100  # this page has 161+ unique external links

    # same-site links (worksheets, answer keys) are excluded by default
    assert not any("allinonehomeschool.com" in u for u in urls)

    # at least some links carry a day_context
    assert any(link.day_context is not None for link in links)


def test_extract_links_strong_variant_algebra_1():
    page = _load("highschool_algebra-1-2023-update.json")
    links = extract_links(
        page["content_html"],
        page_url=page["link"],
        site_base_url="https://allinonehighschool.com",
    )
    urls = {link.url for link in links}

    assert len(links) == len(urls)
    assert len(links) > 50

    # cross-site link to the sister domain counts as external and is kept
    assert any("allinonehomeschool.com" in u for u in urls)

    # day_context capture also works with <strong id="dayN"> markers, not just <div>
    assert any(link.day_context is not None for link in links)


def test_extract_links_drops_fragments_mailto_and_javascript():
    html = """
    <div>
      <a href="#top">top</a>
      <a href="mailto:someone@example.com">email</a>
      <a href="javascript:void(0)">js</a>
      <a href="https://external.example.com/resource">real link</a>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert [link.url for link in links] == ["https://external.example.com/resource"]


def test_extract_links_relative_urls_resolved_against_page_and_excluded_if_same_host():
    html = """
    <div>
      <a href="/worksheet.pdf">same-site relative</a>
      <a href="https://other.example.com/x">other site</a>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert [link.url for link in links] == ["https://other.example.com/x"]


def test_extract_links_day_context_tracks_nearest_preceding_marker():
    html = """
    <div id="day1"><a href="https://ext.example.com/a">a</a></div>
    <div id="day2"><a href="https://ext.example.com/b">b</a></div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    by_url = {link.url: link.day_context for link in links}
    assert by_url["https://ext.example.com/a"] == "day1"
    assert by_url["https://ext.example.com/b"] == "day2"


def test_extract_links_repairs_single_slash_scheme():
    # a real malformed href seen in the wild: "http:/host" with one slash - it must be
    # repaired to a valid absolute URL, not silently dropped or checked as-is
    html = '<a href="http:/example.com/x">bad</a>'
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert [link.url for link in links] == ["http://example.com/x"]


def test_extract_links_same_host_exclusion_is_case_insensitive():
    # a same-site link whose host only differs in case must still be excluded, not
    # mistaken for an external link
    html = '<a href="https://MySite.Example.com/leaf">same site, mixed case</a>'
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links == []
