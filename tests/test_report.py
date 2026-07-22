from datetime import UTC, datetime, timedelta

import pytest

from linkcheck import db, report
from linkcheck.checker import CheckResult, get_due_links, record_check
from linkcheck.config import UNCONFIRMED_RETRY_MINUTES
from linkcheck.crawler import CoursePage, ExtractedLink, sync_course_page


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


def _sync(conn, site_slug, slug, url, links, *, kind="course", sort_order=None):
    page = CoursePage(wp_id=1, slug=slug, canonical_url=url, title=slug.replace("-", " ").title(), html="")
    sync_course_page(conn, site_slug, page, links, kind=kind, sort_order=sort_order)


def _check_all_due(conn, result: CheckResult, now: datetime | None = None):
    now = now or datetime.now(UTC)
    for link in get_due_links(conn, now, batch_size=1000):
        record_check(conn, link, result, now)


def _confirm_broken(conn, result: CheckResult = CheckResult(404, None, 10)):
    """Drive a link through the full unconfirmed-retry schedule to a confirmed status,
    advancing the clock between checks so each retry is actually due (mirrors the
    pattern in test_checker_db.py).
    """
    now = datetime.now(UTC)
    for _ in range(len(UNCONFIRMED_RETRY_MINUTES) + 1):
        _check_all_due(conn, result, now)
        now += timedelta(days=999)


def test_get_site_summaries_counts_by_status(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="a", day_context="day1")],
    )
    _confirm_broken(conn)

    summaries = {(s.slug, s.kind): s for s in report.get_site_summaries(conn)}
    assert summaries[("homeschool", "course")].broken == 1
    assert summaries[("homeschool", "course")].total == 1
    assert summaries[("highschool", "course")].total == 0  # site with no crawled pages yet
    assert summaries[("homeschool", "other")].total == 0  # no 'other' pages crawled yet


def test_get_site_summaries_splits_course_and_other(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/course-broken", text="a", day_context=None)],
        kind="course", sort_order=0,
    )
    _sync(
        conn, "homeschool", "odd-and-even", "https://allinonehomeschool.com/odd-and-even/",
        [ExtractedLink(url="https://ext.example.com/other-broken", text="b", day_context=None)],
        kind="other",
    )
    _confirm_broken(conn)

    summaries = {(s.slug, s.kind): s for s in report.get_site_summaries(conn)}
    assert summaries[("homeschool", "course")].broken == 1
    assert summaries[("homeschool", "course")].total == 1
    assert summaries[("homeschool", "other")].broken == 1
    assert summaries[("homeschool", "other")].total == 1


def test_get_problem_links_excludes_ok_and_pending(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [
            ExtractedLink(url="https://ext.example.com/ok", text="ok", day_context=None),
            ExtractedLink(url="https://ext.example.com/pending", text="pending", day_context=None),
        ],
    )
    now = datetime.now(UTC)
    due = get_due_links(conn, now, batch_size=1000)
    ok_link = next(link for link in due if link.url.endswith("/ok"))
    record_check(conn, ok_link, CheckResult(200, None, 10), now)
    # leave /pending untouched -> stays status='pending'

    problem_links = report.get_problem_links(conn)
    assert problem_links == []


def test_get_problem_links_includes_page_context(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="link text", day_context="day5")],
    )
    _confirm_broken(conn)

    problem_links = report.get_problem_links(conn)
    assert len(problem_links) == 1
    link = problem_links[0]
    assert link.status == "broken"
    assert link.last_http_status == 404
    assert len(link.pages) == 1
    assert link.pages[0].site_slug == "homeschool"
    assert link.pages[0].day_context == "day5"
    assert link.pages[0].page_title == "Math 1"


def test_get_problem_links_shared_link_lists_every_page(conn):
    shared = ExtractedLink(url="https://ext.example.com/shared", text="shared", day_context=None)
    _sync(conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/", [shared])
    _sync(conn, "homeschool", "math-2", "https://allinonehomeschool.com/math-2/", [shared])

    _confirm_broken(conn)

    problem_links = report.get_problem_links(conn)
    assert len(problem_links) == 1
    assert {p.page_title for p in problem_links[0].pages} == {"Math 1", "Math 2"}


def test_get_watch_links_includes_first_failure_not_yet_confirmed(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/flaky", text="flaky", day_context="day2")],
    )
    _check_all_due(conn, CheckResult(404, None, 10))  # single failure, unconfirmed

    assert report.get_problem_links(conn) == []  # not confirmed -> not a "problem" yet
    watch_links = report.get_watch_links(conn)
    assert len(watch_links) == 1
    link = watch_links[0]
    assert link.url == "https://ext.example.com/flaky"
    assert link.consecutive_failures == 1
    assert link.status == "pending"  # underlying status unchanged, per next_state()
    assert link.pages[0].day_context == "day2"


def test_get_watch_links_excludes_orphaned_links(conn):
    # A link that failed a check and then lost every page association (e.g. the
    # course page was recrawled and no longer contains it) is also excluded from the
    # check queue going forward - so without this filter it would sit in "watching"
    # forever with no page to point at and no future check that could ever confirm
    # or clear it. See report._link_rows.
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/flaky", text="flaky", day_context=None)],
    )
    _check_all_due(conn, CheckResult(404, None, 10))  # single failure, unconfirmed
    assert len(report.get_watch_links(conn)) == 1

    link_id = conn.execute("SELECT id FROM links").fetchone()["id"]
    conn.execute("DELETE FROM page_links WHERE link_id = ?", (link_id,))
    conn.commit()

    assert report.get_watch_links(conn) == []


def test_get_problem_links_excludes_orphaned_links(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="broken", day_context=None)],
    )
    _confirm_broken(conn)
    assert len(report.get_problem_links(conn)) == 1

    link_id = conn.execute("SELECT id FROM links").fetchone()["id"]
    conn.execute("DELETE FROM page_links WHERE link_id = ?", (link_id,))
    conn.commit()

    assert report.get_problem_links(conn) == []


@pytest.mark.parametrize("text", ["source", "Source", "(source)", "source)", "  source  "])
def test_get_problem_links_excludes_source_citation_links(conn, text):
    # Both sites mark citation/attribution links with literal anchor text "source"
    # (styled and disclaimed as "do not click" in the page content itself) - these
    # should never show up as something to fix. See config.BLACKLIST_RULES.
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/citation", text=text, day_context=None)],
    )
    _confirm_broken(conn)

    assert report.get_problem_links(conn) == []
    summaries = {(s.slug, s.kind): s for s in report.get_site_summaries(conn)}
    assert summaries[("homeschool", "course")].broken == 0
    assert summaries[("homeschool", "course")].total == 0


def test_get_problem_links_keeps_source_text_link_if_used_as_a_real_link_elsewhere(conn):
    # The same URL might be a throwaway citation on one page but a genuine course link
    # on another - only suppress it if *every* reference to it uses the citation text.
    shared = ExtractedLink(url="https://ext.example.com/shared", text="source", day_context=None)
    real = ExtractedLink(url="https://ext.example.com/shared", text="Read this", day_context=None)
    _sync(conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/", [shared])
    _sync(conn, "homeschool", "math-2", "https://allinonehomeschool.com/math-2/", [real])
    _confirm_broken(conn)

    problem_links = report.get_problem_links(conn)
    assert len(problem_links) == 1


def test_get_watch_links_excludes_confirmed_and_healthy_links(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [
            ExtractedLink(url="https://ext.example.com/broken", text="broken", day_context=None),
            ExtractedLink(url="https://ext.example.com/ok", text="ok", day_context=None),
        ],
    )
    now = datetime.now(UTC)
    for _ in range(len(UNCONFIRMED_RETRY_MINUTES) + 1):
        for link in get_due_links(conn, now, batch_size=1000):
            result = CheckResult(404, None, 10) if "broken" in link.url else CheckResult(200, None, 10)
            record_check(conn, link, result, now)
        now += timedelta(days=999)

    assert report.get_watch_links(conn) == []  # one confirmed broken, one steadily ok


def test_get_site_summaries_watching_count(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/flaky", text="flaky", day_context=None)],
    )
    _check_all_due(conn, CheckResult(404, None, 10))

    summaries = {(s.slug, s.kind): s for s in report.get_site_summaries(conn)}
    assert summaries[("homeschool", "course")].watching == 1
    assert summaries[("homeschool", "course")].pending == 1  # still counted under its real status too


def test_get_check_progress_counts_checked_vs_total(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [
            ExtractedLink(url="https://ext.example.com/checked", text="a", day_context=None),
            ExtractedLink(url="https://ext.example.com/pending", text="b", day_context=None),
        ],
    )
    now = datetime.now(UTC)
    due = get_due_links(conn, now, batch_size=1000)
    checked_link = next(link for link in due if link.url.endswith("/checked"))
    record_check(conn, checked_link, CheckResult(200, None, 10), now)
    # leave /pending untouched

    progress = report.get_check_progress(conn)
    assert progress.checked == 1
    assert progress.total == 2
    assert progress.pct == 50.0


def test_get_check_progress_excludes_orphaned_links(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/gone", text="a", day_context=None)],
    )
    link_id = conn.execute("SELECT id FROM links").fetchone()["id"]
    conn.execute("DELETE FROM page_links WHERE link_id = ?", (link_id,))
    conn.commit()

    progress = report.get_check_progress(conn)
    assert progress.total == 0  # orphaned link isn't part of the checkable population
    assert progress.pct == 0.0  # no division by zero


def test_render_text_report_no_problems(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/ok", text="ok", day_context=None)],
    )
    _check_all_due(conn, CheckResult(200, None, 10))

    text = report.render_text_report(
        report.get_site_summaries(conn), report.get_problem_links(conn), report.get_watch_links(conn)
    )
    assert "No broken or unreachable links." in text
    assert "homeschool" in text


def test_render_text_report_lists_problems(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="a", day_context="day3")],
    )
    _confirm_broken(conn)

    text = report.render_text_report(
        report.get_site_summaries(conn), report.get_problem_links(conn), report.get_watch_links(conn)
    )
    assert "https://ext.example.com/broken" in text
    assert "day3" in text
    assert "broken" in text


def test_render_text_report_lists_watching(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/flaky", text="a", day_context="day7")],
    )
    _check_all_due(conn, CheckResult(404, None, 10))

    text = report.render_text_report(
        report.get_site_summaries(conn), report.get_problem_links(conn), report.get_watch_links(conn)
    )
    assert "watching" in text
    assert "https://ext.example.com/flaky" in text
    assert "day7" in text


def test_render_html_report_contains_expected_content(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="a", day_context="day3")],
    )
    _confirm_broken(conn)

    html = report.render_html_report(
        report.get_problem_links(conn),
        report.get_watch_links(conn),
    )
    assert "<html" in html
    assert "https://ext.example.com/broken" in html
    assert "Math 1" in html


def test_render_html_report_separates_other_pages_section(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/course-broken", text="a", day_context=None)],
        kind="course", sort_order=0,
    )
    _sync(
        conn, "homeschool", "odd-and-even", "https://allinonehomeschool.com/odd-and-even/",
        [ExtractedLink(url="https://ext.example.com/other-broken", text="b", day_context=None)],
        kind="other",
    )
    _confirm_broken(conn)

    html = report.render_html_report(report.get_problem_links(conn), report.get_watch_links(conn))
    assert "Other pages" in html
    assert "Math 1" in html
    assert "Odd And Even" in html
    # course group renders before the "Other pages" divider, which renders before the
    # 'other' group
    assert html.index("Math 1") < html.index("Other pages") < html.index("Odd And Even")


def test_render_html_report_no_other_pages_divider_when_all_course(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/broken", text="a", day_context=None)],
    )
    _confirm_broken(conn)

    html = report.render_html_report(report.get_problem_links(conn), report.get_watch_links(conn))
    assert "Other pages" not in html


def test_dashboard_shows_blacklist_configuration():
    html = report.render_html_report([], [])
    assert "Never-checked hosts" in html
    assert "web.archive.org" in html
    assert "Source-citation link text" in html


def test_render_html_report_shows_watching_section(conn):
    _sync(
        conn, "homeschool", "math-1", "https://allinonehomeschool.com/math-1/",
        [ExtractedLink(url="https://ext.example.com/flaky", text="a", day_context=None)],
    )
    _check_all_due(conn, CheckResult(404, None, 10))

    html = report.render_html_report(
        report.get_problem_links(conn),
        report.get_watch_links(conn),
    )
    assert "Watching" in html
    assert "https://ext.example.com/flaky" in html
    assert "status-watching" in html


def test_render_html_report_empty_state(conn):
    html = report.render_html_report([], [])
    assert "No broken or unreachable links." in html
    assert "Watching (" not in html  # section itself is skipped when watch_links is empty


def _entry(day_context=None, day_label=None, link_text=None, context_before=None, context_after=None):
    return report.PageGroupEntry(
        link=None,
        day_context=day_context,
        day_number=int(day_context[3:]) if day_context else None,
        day_label=day_label,
        link_text=link_text,
        context_before=context_before,
        context_after=context_after,
    )


_GROUP = report.PageGroup(
    site_slug="highschool",
    page_title="Chemistry",
    page_url="https://allinonehighschool.com/chemistry/",
    last_crawled_at=None,
    kind="course",
    entries=[],
)


def test_found_on_href_bare_page_when_nothing_known():
    assert report.found_on_href(_GROUP, _entry()) == _GROUP.page_url


def test_found_on_href_day_context_only():
    href = report.found_on_href(_GROUP, _entry(day_context="day3"))
    assert href == f"{_GROUP.page_url}#day3"


def test_found_on_href_text_fragment_only():
    href = report.found_on_href(_GROUP, _entry(link_text="a fairly long chunk of anchor text"))
    assert href == f"{_GROUP.page_url}#:~:text=a%20fairly%20long%20chunk%20of%20anchor%20text"


def test_found_on_href_combines_day_context_and_text_fragment():
    href = report.found_on_href(_GROUP, _entry(day_context="day3", link_text="a fairly long anchor phrase"))
    assert href == f"{_GROUP.page_url}#day3:~:text=a%20fairly%20long%20anchor%20phrase"


def test_found_on_href_skips_text_fragment_below_word_threshold():
    # a single short word with no surrounding prose (e.g. a bare "Soviet" list item) is
    # too short/generic a directive to trust - day_context alone is used instead
    href = report.found_on_href(_GROUP, _entry(day_context="day92", link_text="Soviet"))
    assert href == f"{_GROUP.page_url}#day92"


def test_found_on_href_skips_text_fragment_below_word_threshold_falls_back_to_bare_page():
    # same, but with no day_context either - falls all the way back to the bare page
    href = report.found_on_href(_GROUP, _entry(link_text="Soviet"))
    assert href == _GROUP.page_url


def test_found_on_href_counts_context_words_toward_the_threshold():
    # link_text alone is short, but enough surrounding prose pushes the total over
    # the threshold, so the fragment is still used
    href = report.found_on_href(
        _GROUP,
        _entry(link_text="source", context_before="check out this great", context_after="for more information"),
    )
    assert ":~:text=" in href


def test_found_on_href_uses_text_fragment_right_at_word_threshold():
    href = report.found_on_href(_GROUP, _entry(link_text="one two three four five"))
    assert ":~:text=" in href


def test_found_on_href_skips_text_fragment_just_below_word_threshold():
    href = report.found_on_href(_GROUP, _entry(link_text="one two three four"))
    assert ":~:text=" not in href


def test_found_on_href_adds_prefix_and_suffix_context():
    href = report.found_on_href(
        _GROUP,
        _entry(link_text="source", context_before="see the", context_after="for more detail"),
    )
    assert href == f"{_GROUP.page_url}#:~:text=see%20the-,source,-for%20more%20detail"


def test_found_on_href_escapes_hyphens_and_commas_in_context():
    # hyphens and commas are meaningful in the text directive's own mini-syntax and
    # must be escaped even inside the encoded text itself, not just as separators
    href = report.found_on_href(
        _GROUP,
        _entry(link_text="non-fiction, sort of", context_before="a-b", context_after="c,d"),
    )
    assert "non%2Dfiction%2C%20sort%20of" in href
    assert "a%2Db-," in href
    assert ",-c%2Cd" in href
    assert href.count("%2D") == 2  # both literal hyphens escaped, not just the separators
