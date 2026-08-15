from linkcheck.config import (
    BLACKLIST_RULES,
    HostSuffixBlacklistRule,
    PageBlacklistRule,
    blacklisted_page_slugs,
)


def test_blacklist_rule_keys_are_unique():
    # Each rule's key namespaces its own SQL param names and (for LinkTextBlacklistRule)
    # its EXISTS-subquery alias - a duplicate key would silently collide when
    # exclusion_clause() folds multiple rules into one combined SQL fragment.
    keys = [rule.key for rule in BLACKLIST_RULES]
    assert len(keys) == len(set(keys))


def test_page_blacklist_rules_contribute_no_sql_clause():
    # PageBlacklistRule is enforced by crawler.crawl_site skipping the slug outright,
    # not by filtering check/report queries - it must never contribute a WHERE fragment.
    page_rules = [rule for rule in BLACKLIST_RULES if isinstance(rule, PageBlacklistRule)]
    assert page_rules
    for rule in page_rules:
        assert rule.sql_clause() == ("", {})


def test_blacklisted_page_slugs_matches_configured_rules():
    expected = set()
    for rule in BLACKLIST_RULES:
        if isinstance(rule, PageBlacklistRule):
            expected |= rule.values
    assert blacklisted_page_slugs() == expected
    assert "parent-submitted-courses" in expected
    assert "foresensics-parent-submitted" in expected


def test_host_suffix_blacklist_rule_matches_domain_and_subdomains_only():
    import sqlite3

    rule = HostSuffixBlacklistRule(
        key="test_suffix",
        label="test",
        reason="test",
        kind="host suffix",
        values=frozenset({"archive.org"}),
    )
    clause, params = rule.sql_clause(host_column="host", link_id_column="1")
    hosts = [
        "archive.org",
        "www.archive.org",
        "ia801603.us.archive.org",
        "poetryarchive.org",
        "myarchive.org",
        "notarchive.org.evil.com",
        "ext.example.com",
    ]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (host TEXT)")
    conn.executemany("INSERT INTO t (host) VALUES (?)", [(h,) for h in hosts])
    remaining = {
        row[0] for row in conn.execute(f"SELECT host FROM t WHERE 1=1 {clause}", params)
    }
    assert remaining == {"poetryarchive.org", "myarchive.org", "notarchive.org.evil.com", "ext.example.com"}
