# Implement the Raindrop RSS bridge

Date: 2026-09-08
Status: steps 1-4 done; step 5 (Geekom live smoke) not started and needs host access, credentials, and a Telegram profile for `tg`.
Behavior: [specification](../spec/raindrop-rss.md). Status ledger: [TODO](TODO.md).

## 1. Create the minimal application

Use Python, an established RSS/Atom parser, an HTTP client with timeouts, standard-library JSON/TOML/SQLite support, and a minimal HTTP server. Pin dependencies. Keep a small package with configuration, ingestion/state, and Raindrop/notification adapters; split further only when it improves clarity. Add example settings and `feeds.json`, secret exclusions, and a Dockerfile targeting the Geekom's Linux amd64 architecture.

Implement config validation, normal service mode, and `--once --dry-run`. Persist a new feed's addition cutoff before its first network fetch. Use one writable data directory and an exclusive process lock. Implement the spec’s file reload and validation rules, retaining the last valid subscriptions after invalid edits. Keep runtime state in SQLite and subscriptions in JSON.

Done: config loads without secrets in source, cutoff persists on failure/restart, and dry-run changes no production state or remote data.

## 2. Add the minimal feed GUI

Implement the specification's single server-rendered page and add form. Keep the configured listener (default port `38471`) responsive while the sequential ingestion loop runs in the same process; serialize state mutations. Persist feed check/success/publication timestamps and errors for the status rows, and query delivery counts separately. Use text labels alongside health symbols and refresh the page periodically.

Write additions atomically to the mounted feed-file directory, protecting against conflicting manual edits and reporting validation/write failures. Register each new cutoff before fetching. Keep manual edit/removal and portable export in the JSON file; no additional management screens.

Done: fixture tests cover status transitions, never-checked and quiet feeds, additions/restarts, manual reload, invalid files, duplicates, conflicting edits, and failed writes. A slow mocked publisher does not prevent the page from responding, and dry-run opens no port.

## 3. Implement the hourly ingestion pass

Parse RSS/Atom and publication dates, resolve article URLs, enforce the strict cutoff, deduplicate URLs locally, and persist candidates. Create bookmarks in the configured collection with source tags. Verify current request/response and rate-limit semantics against Raindrop's official API docs during implementation.

Implement the four delivery states and next-pass retries from the spec. Recover interrupted submissions into manual review. Provide a short operator command or documented helper for resolving a single review row after checking Raindrop; avoid a general administration interface.

Done: deterministic fixture/fake HTTP tests cover the ingestion and state acceptance criteria, particularly old article updates, feed rollover, cross-feed duplicates, timeout after remote creation, bad credentials, and a second writer.

## 4. Package Docker and Telegram

Confirm the Geekom's `tg` implementation and dependencies, then make that command work inside the container. Prefer packaging the script with protected runtime credentials if portable. If it requires host-only facilities, return to the design rather than silently adding SSH, a host agent, or Docker socket access.

Add the hourly sequential loop, graceful termination, persistent incident suppression, and aggregate failure/recovery alerts. Add Compose with restart policy, read-only settings/secrets, writable feed-file directory, persistent SQLite data, a configurable GUI port published on host loopback, and rotated logs. Use a short README for configuration, token setup, GUI access (including an SSH tunnel), manual feed edits and reload timing, commands, review-row recovery, and stop/copy/start backup.

Done: a container restart preserves cutoffs, queue, and incident state; notification tests verify argument handling, timeout, redaction, suppression, and retry after notification failure. Actual `tg` delivery requires a configured test environment.

## 5. Smoke-test on the Geekom

With host access and credentials supplied, preview the real configuration, import a controlled newly published item into an existing test collection, rerun to check deduplication, and test a failure/recovery Telegram message. Confirm GUI access on the configured port during and between passes, add a feed through the form, verify its JSON entry and status, and confirm one hourly pass and host reboot persistence. Then configure the real RSS Inbox and all feeds.

Run the deterministic suite once after implementation; repeat checks only for changed behavior or new failures. Record the results and any outstanding live checks. Do not report live deployment complete based on mocks.

Done: all specification acceptance criteria have evidence, setup is documented, and pending operator decisions are resolved. This documentation request does not itself perform deployment.

## Implementation status

Done (2026-09-08):

- **Step 1.** `raindrop_rss/` holds `config.py`, `state.py`, `adapters.py`, `bridge.py`, `gui.py`, `__main__.py`. `Dockerfile`, `compose.yaml`, `config/settings.example.toml`, `subscriptions/feeds.example.json`, `.gitignore` and `.dockerignore` are in place. Both proposed spec defaults are adopted: undated entries are skipped and reported as per-feed counts, and alerts run a host command named by `tg_command`.
- **Step 2.** One server-rendered page with the add form below the list, a per-process form token, bounded form length and socket timeouts, and a 30-second refresh. Status comes from SQLite; the listener runs in a background thread while ingestion holds the main thread.
- **Step 3.** Sequential passes with a fetch/deliver budget split, strict publication cutoffs, cross-feed URL deduplication, the four delivery states, and `review list | saved | requeue` for one row at a time.
- **Step 4.** Alerts are a host command. A missing command does not stop the pass. Persistent incident state, aggregate failure/recovery alerts, `restart: unless-stopped`, loopback port publishing, and rotated logs are in place.

Reviewed and corrected 2026-09-09: generated feed IDs now avoid every ID SQLite
has seen, so a reused tag cannot inherit a retired cutoff; a long `Retry-After` is
honoured in full instead of capped; a refused or DNS-failed connection stays
`pending` instead of going to review; `--dry-run review` is rejected before any
file or lock is opened; `--debug` no longer raises urllib3's request logging and
the GUI logs no request paths; Compose runs as a configurable host UID/GID with a
parametrised port; the add form moved to its own non-refreshing `/add` page and
preserves the feed file's permissions; an invalid file at startup pauses queued
remote writes too; and a stale `remote_error` clears once nothing is outstanding.

Also corrected 2026-09-09: `HTTP.request` bounded only the final response, so
requests' eager read of an intermediate redirect body bypassed `max_feed_bytes`
entirely (a 302 carrying 65536 bytes passed a 1024-byte limit). A `requests`
response hook now bounds and caches every hop's body before requests consumes it
-- which also covers `allow_redirects=False`, where requests still reads the body
to prepare `_next` -- with one cumulative byte budget for the chain. The absolute
SIGALRM deadline, redirect credential stripping, and the no-redirect-on-POST rule
are unchanged.

Evidence (2026-09-09, logs under `/tmp/external-worker/evidence.HAZfeq/`):

- `.venv/bin/python -m unittest discover -s tests -t .` — 152 tests, exit 0
  (`10-unittest-full.log`, rerun in `/tmp/external-worker/evidence.HAZfeq/42-tests-after-fix.log`), including
  `tests/test_regressions.py` for each reviewed finding and `tests/test_http.py`
  for bounded reads against a real local server.
- Redirect-body bypass reproduced before the fix and refused after it
  (`/tmp/external-worker/evidence.HAZfeq/40-repro-before.log`, `41-repro-after.log`). `docker build --platform
  linux/amd64 -t raindrop-rss:1 .` exit 0, and `tests.test_http` runs 9/9 inside
  that image with `--network none` (`/tmp/external-worker/evidence.HAZfeq/43-image-http-tests.log`).
- `docker build --platform linux/amd64 -t raindrop-rss:2 .` exit 0, `linux/amd64`
  image, no Dockerfile lint warning (`20-docker-build.log`); `docker compose config`
  exit 0 resolving `.env` into `user: 1000:1000` and port 9111 (`25-...`).
- Containerised fixture smoke as the host UID with mode-600 dummy secrets and both
  `api.raindrop.io` and `api.telegram.org` pinned to `0.0.0.0`, so no live call is
  possible: the container reads the mode-600 token and writes both mounts (`21-...`);
  pass 1 registers cutoffs and imports nothing, then an article published *after*
  that cutoff is queued while a day-old one and an undated one are skipped
  (`22-...`); a refused Raindrop connection leaves the row `pending`, `--dry-run
  review` exits 2 without touching the database, and a dry run leaves it
  byte-for-byte identical (`23-...`); the status page carries no form, `/add`
  carries no refresh, a GUI addition writes `example-2` and keeps mode 600, a
  non-ASCII token is rejected with HTTP 200, and `--debug` logs leak neither the
  feed query secret nor any request path (`24-...`); SIGTERM stops the container in
  0.3 s after closing the listener (`25-...`).

Not done:

- **Step 5.** No live Raindrop call, no Telegram message, and no deployment have been performed. See the open items in [TODO](TODO.md).

## Keep the scope small

Defer automatic remote reconciliation, conditional HTTP caching, OPML import unless requested, multiple deployment targets, collection creation, full-text extraction, and external uptime monitoring. Preserve the essential protections: durable pending items, publication cutoffs, duplicate history, and visible ambiguous saves.

Update the ledger as work progresses. Do not move this plan to `completed/` until step 5 has real evidence; the deterministic suite does not substitute for the live smoke test.
