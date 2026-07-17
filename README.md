# linkcheck

A background worker that crawls the curriculum pages of [allinonehomeschool.com](https://allinonehomeschool.com)
and [allinonehighschool.com](https://allinonehighschool.com)
and checks the external links they contain for breakage, using SQLite as both the data
store and the check-phase work queue.

This codebase is built primarily by Claude, and the code quality will likely reflect it.

## What it does

- **Crawls** each site's course index page to discover course pages, then pulls each
  course page's full body via the WordPress REST API and extracts every external link
  (including cross-site links between the two domains - excludes only same-site
  self-links like PDFs/answer keys, for now).
- **Diffs** each recrawl against what's stored: new links are added, links no longer on
  a page have that association dropped (the link itself is kept, not hard-deleted, in
  case it reappears).
- **Checks** links on their own independent schedule, pulled from the database as a
  due-time queue - crawling and checking never block each other. A single 404 (today's
  definition of "broken") doesn't immediately flag a link; a failure has to survive a
  short confirm-before-flagging retry schedule first, to rule out a transient blip.
- **Reports** current status as a terminal table or a static, dependency-free HTML
  dashboard, regenerated automatically at the end of every check cycle.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Quick start

```sh
uv sync
uv run linkcheck init-db                 # create tables, seed site config
uv run linkcheck crawl --limit 5         # crawl a few course pages per site, for a quick look
uv run linkcheck check --batch-size 50   # check whatever's due
uv run linkcheck report --html public/status.html
```

Or just run the whole thing as a long-lived worker (crawl loop + check loop together,
until Ctrl-C):

```sh
uv run linkcheck run
```

## CLI reference

| command | what it does |
|---|---|
| `init-db` | Create tables (if missing) and sync site config into the database. |
| `discover-courses` | Print the course pages found on each site's index page. Read-only, doesn't touch the DB. |
| `crawl-preview` | Fetch a few real course pages and print extracted links. Read-only, doesn't touch the DB. |
| `crawl [--limit N]` | Crawl all course pages for both sites and sync links into the database. |
| `check [--batch-size N]` | Check one batch of due links and record results. |
| `report [--html PATH]` | Print a text report; optionally also render the static HTML dashboard. |
| `run [--dashboard-path PATH]` | Run the background worker: crawl loop + check loop, until interrupted. |

All commands accept `--db-path` (default `linkcheck.db` in the current directory).

## Configuration

Site definitions and every tuning constant (crawl interval, check batch size,
concurrency caps, timeouts, the confirm-before-flagging retry schedule, healthy/broken
recheck intervals) live in `src/linkcheck/config.py`. There's no environment-variable
layer on top of it - change a value there and redeploy.

## Data model

Five tables: `sites`, `pages`, `links`, `page_links` (the many-to-many join, since a
link can appear on multiple course pages), and `link_checks` (append-only history of
every check). "Broken" is a classifier (`checker.classify()`), not a stored judgment -
raw HTTP status/error type is always recorded, so redefining "broken" later only means
editing that one function and reclassifying history, never re-checking anything.

## Running as a background worker

A systemd unit is provided at [`deploy/linkcheck.service`](deploy/linkcheck.service).

```sh
# on the deploy host
sudo useradd --system --home /opt/linkcheck --shell /usr/sbin/nologin linkcheck
sudo git clone <this repo> /opt/linkcheck   # or however you get the code there
cd /opt/linkcheck
sudo -u linkcheck uv sync                   # builds /opt/linkcheck/.venv
sudo -u linkcheck uv run linkcheck init-db --db-path /var/lib/linkcheck/linkcheck.db

sudo cp deploy/linkcheck.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now linkcheck
sudo systemctl status linkcheck
journalctl -u linkcheck -f
```

The unit runs `.venv/bin/linkcheck` directly rather than `uv run`, so nothing re-resolves
dependencies on every restart. `StateDirectory=linkcheck` makes systemd create
`/var/lib/linkcheck` (owned by the `linkcheck` user) automatically on first start.

### Serving the dashboard

`status.html` is a fully static file (no server-side code, no API calls) written to
`/var/lib/linkcheck/public/status.html` by the unit above, alongside the logo it
references at `/var/lib/linkcheck/public/assets/` (synced there by the unit's
`ExecStartPre` on every start). Point any static file server at the `public/` dir. For
example, with nginx:

```nginx
location /linkcheck/ {
    alias /var/lib/linkcheck/public/;
}
```

Or, for a quick local look without setting up a real web server:

```sh
python -m http.server --directory /var/lib/linkcheck/public 8000
```

## Development

```sh
uv run pytest
```

Tests run offline against saved fixture HTML (`tests/fixtures/`) and an in-memory
SQLite database - no network access required. The one exception is manual, ad hoc live
verification against the real sites during development (`crawl-preview`,
`discover-courses`), which is why those commands exist as read-only sanity checks
separate from the test suite.

## Project layout

```
src/linkcheck/
  config.py       # site definitions + every tuning constant
  db.py            # schema init + connection helpers
  crawler.py        # course discovery, page fetch, link extraction, upsert/diff
  checker.py          # classify(), backoff scheduling, concurrent HTTP checks
  scheduler.py          # crawl loop + check loop running together
  report.py              # query layer, text report, HTML dashboard rendering
  cli.py                  # command-line entry points
  schema.sql               # table definitions
  templates/
    status.html.jinja       # dashboard template
tests/
  fixtures/                 # real saved HTML/JSON used by offline tests
deploy/
  linkcheck.service          # systemd unit
public/                       # rendered dashboard lands here (gitignored)
  assets/
    ep-logo.jpg                 # static asset the dashboard template references
```
