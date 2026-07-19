from linkcheck.config import BLACKLIST_RULES


def test_blacklist_rule_keys_are_unique():
    # Each rule's key namespaces its own SQL param names and (for LinkTextBlacklistRule)
    # its EXISTS-subquery alias - a duplicate key would silently collide when
    # exclusion_clause() folds multiple rules into one combined SQL fragment.
    keys = [rule.key for rule in BLACKLIST_RULES]
    assert len(keys) == len(set(keys))
