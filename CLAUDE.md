# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
uv sync                                  # install deps
uv run linkcheck init-db                 # create tables, seed site config
uv run linkcheck crawl --limit 5         # crawl a few course pages per site
uv run linkcheck check --batch-size 50   # check whatever's due
uv run linkcheck report --html public/status.html
uv run linkcheck run                     # long-lived worker: crawl loop + check loop together

uv run pytest                            # full test suite (offline, fixtures + in-memory sqlite)
uv run pytest tests/test_checker.py      # single file
uv run pytest tests/test_checker.py::test_name -v   # single test
```

`discover-courses` and `crawl-preview` are read-only, no-DB commands for manual live
verification against the real sites during development — not part of the test suite.

All commands accept `--db-path` (default `linkcheck.db`).

## Architecture

Two decoupled phases sharing a SQLite DB that is *also* the work queue (no separate queue
system): **crawl** discovers/updates what to check; **check** decides when each link is
next due, on its own per-link schedule. They never block each other.

- `config.py` — site definitions + every tuning constant (crawl interval, check batch
  size, concurrency caps, timeouts, confirm-before-flagging retry schedule, healthy/broken
  recheck intervals). No env-var layer; change values here and redeploy.
  `BLACKLIST_RULES` is the standardized mechanism for links that should never be
  checked or reported at all (by host or by anchor text) — hardcoded, not DB-backed;
  `exclusion_clause()` folds every rule into one SQL fragment spliced into both the
  check-phase queries (`checker.py`) and the report queries (`report.py`), and the
  same rules are listed on the dashboard.
- `db.py` — schema init + connection helpers.
- `crawler.py` — course discovery (scrape index page), page fetch (WP REST API
  `/wp-json/wp/v2/pages?slug=...` → `content.rendered`), link extraction (BeautifulSoup,
  every `<a href>` in the body, best-effort `day_context` from nearest `id="dayN"`),
  upsert/diff into `pages`/`links`/`page_links`.
- `checker.py` — streaming `GET` (never `HEAD` — many sites handle it inconsistently) with
  redirects followed; `classify(http_status, error_type) -> ok | broken | unreachable` is
  the single pure function that decides what counts as broken (raw outcomes are always
  logged, so redefining "broken" later means editing this function and reclassifying
  history, never re-checking); backoff/`next_check_at` scheduling.
- `scheduler.py` — the `run` worker: crawl loop (daily) + check loop (short interval)
  running together as independent asyncio loops in one process; regenerates the HTML
  dashboard at the end of each check cycle.
- `report.py` — query layer shared by both the terminal report and the static HTML
  dashboard (`templates/status.html.jinja`). Dashboard is fully static (embedded JSON,
  client-side filtering) — no backend, no live queries on page load. Rendered dashboards
  land in `public/` (gitignored); `public/assets/` holds the static files (logo) the
  template references, checked into the repo. After changing `report.py` or
  `templates/status.html.jinja`, regenerate `public/status.html` (`uv run linkcheck
  report --html public/status.html` against the existing `linkcheck.db`) so the user can
  open it in a browser and test.
- `cli.py` — click entry points.

### Data model

`sites`, `pages`, `links`, `page_links` (many-to-many: a link can appear on multiple
course pages, carries `day_context`/`link_text`), `link_checks` (append-only history of
every check, `classified_broken` recorded per-check). A link that no longer appears on any
page has its `page_links` rows dropped but is *not* hard-deleted (kept in case it
reappears; excluded from the check queue since that query joins through `page_links`).

### Check-phase admission control (SQL-driven, not in-process locks)

Per-domain concurrency and rate limiting are enforced via SQL against two tables rather
than semaphores, because SQLite has no row-level locking:

- `domain_state` — one row per host, `last_request_started_at` for rate-limit spacing.
- `domain_claims` — one row per in-flight check, inserted on claim / deleted on
  completion; used both for the concurrency cap (`COUNT(*) WHERE host = ...`) and an
  anti-join to never claim the same link twice.

Claims are optimistic conditional writes checked via `rowcount`, not `SELECT ... FOR
UPDATE` — a lost race just means that candidate is retried next poll. This lets the check
loop be a persistent poll-and-fire-`asyncio.create_task` with no batch/queue: a query that
returns 552 due links all on one slow/rate-limited host (this has happened for real, with
`bible.com`) never blocks unrelated hosts from being checked at full speed in the same run.
A stale `domain_claims` row (past a generous threshold, from a crashed process) is purged
lazily on the next claim attempt rather than reset on startup, since startup reset would
clobber a claim legitimately held by a concurrent invocation (e.g. `linkcheck check`
running alongside a persistent `linkcheck run`) sharing the same DB.

### Confirm-before-flagging

A single bad check never flips a link to `broken`: failures increment
`consecutive_failures` and get rechecked soon (rules out a transient blip) until a
threshold is hit, at which point status becomes `broken` (HTTP-level) or `unreachable`
(network-level, kept distinct) and the link settles into a slower steady-state recheck.

## Notes

- No ORM — stdlib `sqlite3` at this scale.
- No external scheduler dependency by design — one process with two async loops, run via
  the systemd unit in `deploy/linkcheck.service` (uses `.venv/bin/linkcheck` directly, not
  `uv run`, to skip dependency re-resolution on every restart).
