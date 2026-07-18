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

    # the div wraps a "Lesson N" heading, not "day1" - the friendly label should
    # reflect that heading
    day1_links = [link for link in links if link.day_context == "day1"]
    assert day1_links and all(link.day_label == "Lesson 1" for link in day1_links)


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

    # the id sits directly on the <strong> holding the lesson title itself
    day2_links = [link for link in links if link.day_context == "day2"]
    assert day2_links and all(link.day_label == "Lesson 2" for link in day2_links)


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


def test_extract_links_day_label_prefers_lesson_heading_over_topic_heading():
    # some course pages put a topic heading (e.g. "Addition") in its own <strong>
    # before the actual "Lesson N" one - the lesson-numbered candidate should win
    html = """
    <div id="day16">
      <p><strong>Addition</strong></p>
      <p><strong>Lesson 16</strong></p>
      <a href="https://ext.example.com/a">a</a>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_label == "Lesson 16"


def test_extract_links_day_label_trims_trailing_boilerplate():
    # e.g. "Lesson 3* (Note that an asterisk * indicates there is a worksheet)" - only
    # the "Lesson N" portion itself is a useful label, not the repeated boilerplate
    html = """
    <div id="day3">
      <p><strong>Lesson 3* (Note that an asterisk * indicates there is a worksheet)</strong></p>
      <a href="https://ext.example.com/a">a</a>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_label == "Lesson 3*"


def test_extract_links_day_label_ignores_unrelated_numbers_inside_the_lesson_body():
    # a real mismatch seen in the wild: a day marker with no heading of its own, whose
    # body links to some *other* book's "lesson 1" - flattening the marker's entire
    # text (heading search included) would wrongly pick that up as this day's title
    html = """
    <div id="day53">
      <ol>
        <li>Read <a href="https://ext.example.com/book">lesson 1</a> in your new book!</li>
      </ol>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_label is None


def test_extract_links_day_label_falls_back_to_following_siblings():
    # a real markup quirk seen in the wild: an empty id-bearing marker with the actual
    # title in a handful of sibling elements right after it, not a descendant at all
    html = """
    <div id="day134"></div>
    <p><strong>Lesson 134</strong></p>
    <a href="https://ext.example.com/a">a</a>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_label == "Lesson 134"


def test_extract_links_day_label_sibling_search_does_not_cross_into_next_day():
    # day1's marker is empty with no title anywhere in it - the sibling fallback must
    # not walk past day2's own marker and borrow *its* title
    html = """
    <div id="day1"></div>
    <a href="https://ext.example.com/a">a</a>
    <div id="day2"><p><strong>Lesson 2</strong></p></div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_context == "day1"
    assert links[0].day_label is None


def test_extract_links_day_label_supports_legacy_day_n_convention():
    # "Day N" was the naming convention on some courses years ago, before "Lesson N" -
    # not seen live anymore, but the label regex still recognizes it defensively
    html = """
    <div id="day9">
      <p><strong>Day 9</strong></p>
      <a href="https://ext.example.com/a">a</a>
    </div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert links[0].day_label == "Day 9"


def test_extract_links_drops_day_context_when_id_repeats_on_page():
    # a course that reuses "day1" once per week instead of numbering days uniquely -
    # #day1 would take a browser to the first (wrong) occurrence, so links near the
    # later occurrences must not get an anchor that points somewhere else on the page
    html = """
    <div id="day1"><a href="https://ext.example.com/week1">week1 link</a></div>
    <div id="day2"><a href="https://ext.example.com/week1b">week1 day2 link</a></div>
    <div id="day1"><a href="https://ext.example.com/week2">week2 link</a></div>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    by_url = {link.url: link.day_context for link in links}
    assert by_url["https://ext.example.com/week1"] is None
    assert by_url["https://ext.example.com/week2"] is None
    assert by_url["https://ext.example.com/week1b"] == "day2"  # unique id is unaffected


def test_extract_links_captures_surrounding_context():
    html = """
    <p>Read about the causes and symptoms of <a href="https://ext.example.com/fever">fevers</a> here, then take the quiz.</p>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert len(links) == 1
    link = links[0]
    assert link.context_before is not None and link.context_before.endswith("symptoms of")
    assert link.context_after is not None and link.context_after.startswith("here, then take the quiz.")


def test_extract_links_context_does_not_cross_block_boundary():
    html = """
    <p>unrelated preceding paragraph text that should never appear</p>
    <p><a href="https://ext.example.com/x">click here</a></p>
    <p>unrelated following paragraph text that should never appear</p>
    """
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert len(links) == 1
    link = links[0]
    assert link.context_before is None
    assert link.context_after is None


def test_extract_links_context_none_when_link_has_no_text():
    html = '<p>before text <a href="https://ext.example.com/img"><img src="x.png"></a> after text</p>'
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert len(links) == 1
    assert links[0].context_before is None
    assert links[0].context_after is None


def test_extract_links_context_truncation_never_splits_a_word():
    # a real case that broke scroll-to-text-fragment matching: a naive char-count
    # slice landed mid-word ("Read" -> "Re"), and "Re" is no longer literally what a
    # browser's word-boundary-aware text matcher is looking for
    html = (
        "<li>The <a href='https://ext.example.com/subjunctive'>subjunctive</a> "
        "isn’t used very often and is pretty tricky to understand. Read about "
        "the subjunctive here.</li>"
    )
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    assert len(links) == 1
    context_after = links[0].context_after
    assert context_after is not None
    assert not context_after.endswith(" Re")
    assert "Read" in context_after or context_after.endswith("understand.")
    # every word in the truncated snippet must be a complete word from the source text
    assert all(
        word.strip(".,") in "isn’t used very often and is pretty tricky to understand. Read about the subjunctive here."
        for word in context_after.split()
    )


def test_extract_links_context_does_not_invent_whitespace_at_tag_boundaries():
    # a real case that broke scroll-to-text-fragment matching: joining sibling text
    # nodes with an inserted " " separator invents spaces that were never actually on
    # the page ("Act V</a>." -> "Act V ." and "(<a>...</a>)" -> "( ... )"), so the
    # stored context stopped being a literal substring of the real rendered text
    html = (
        "<li>Read <a href='https://ext.example.com/act5'>Act V</a>. You can follow "
        "along with the <a href='https://ext.example.com/audio'>audio for Act V</a> "
        "here. (<a href='https://ext.example.com/alt'>alternate audio</a>)</li>"
    )
    links = extract_links(html, page_url="https://mysite.example.com/course/", site_base_url="https://mysite.example.com")
    by_url = {link.url: link for link in links}

    act5_link = by_url["https://ext.example.com/act5"]
    assert act5_link.context_after is not None
    assert act5_link.context_after.startswith(".")  # no invented space before the period

    audio_link = by_url["https://ext.example.com/audio"]
    assert audio_link.context_after == "here. (alternate audio)"  # no invented spaces inside the parens


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
