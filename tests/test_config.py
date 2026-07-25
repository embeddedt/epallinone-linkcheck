from linkcheck.config import BLACKLIST_RULES, PageBlacklistRule, blacklisted_page_slugs


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
