---
name: flag-review
description: Review long-stuck broken/unreachable links from the live linkcheck dashboard, investigate each one by hand, and propose a BLACKLIST_RULES fix for the ones that deserve it. Run locally, never as an unattended agent - you review everything it proposes before it's committed.
disable-model-invocation: true
---

# flag-review

Never open a PR, commit, or push. Edit the working tree, run the tests, show a diff,
and stop - the person running this decides whether to commit.

## Step 1: Get the data

Fetch `https://epallinone.com/linkcheck/status.json` (or a newer local
`public/status.json`, if present). This is the entire input. Do not query the
production database. Do not request the target URL yourself to "verify" a
connectivity failure - a different network proves nothing about what the checker
sees (see `unreachable_from_checker_host` in `config.py`).

Fields per entry: `status`, `last_error_type`, `last_broken_reason`,
`last_error_detail`, `consecutive_failures`, `last_checked_at`, `status_changed_at`,
`found_on`.

## Step 2: Find what's actually stuck

Filter to entries where:
- `status_changed_at` is set (skip `null`), and
- it's been in that status 2+ weeks, with `last_checked_at` recent.

Use `status_changed_at`, not `consecutive_failures` - it clamps at the confirm
threshold and can't distinguish "broken since yesterday" from "broken since last
month" (see `LinkReportRow.status_changed_at` in `report.py`).

Read `BLACKLIST_RULES` in `src/linkcheck/config.py` first; drop anything already
covered.

## Step 3: Investigate each candidate for real

Don't classify from `last_error_type` alone:
- `curl -v` the URL. For `bad_ssl_cert`, also `openssl s_client -connect host:443
  -servername host` to see the cert chain and SAN.
- Try the plain HTTP request too if `error_type` suggests an HTTPS-upgrade issue.
- Compare `last_error_detail` against what you observe.
- For a rot-heuristic false positive (`last_broken_reason` set), fetch the URL and
  read what's actually there.

Match the evidence bar of the existing `BLACKLIST_RULES` entries: a curl transcript,
a cert SAN mismatch, an explicit "(alternate link)" pairing - not a guess from the
error type. Short of that, put the link in the "needs a human" summary, not
`config.py`.

Exception: a failure that's consistent in the checker's own history but that you
can't reproduce is not evidence it's fine - it's evidence of a network-specific
block (the docsouth.unc.edu shape). Don't downgrade confidence just because your own
request succeeded.

## Step 4: For links worth excluding

- Add to an existing rule's `values` when the category matches (e.g. another host
  blocked from the checker's network goes in `unreachable_from_checker_host`) rather
  than minting a new rule.
- `reason`: 1 sentence, 2 at most, stating the concrete evidence observed - see the
  standing comment above `BLACKLIST_RULES`.
- Run `uv run pytest`. On failure, back the entry out and move the link to "needs a
  human" instead of leaving a red working tree.

## Step 5: Report back

- What changed in `config.py`, if anything - point at the diff.
- Every candidate reviewed but not touched, and why.
