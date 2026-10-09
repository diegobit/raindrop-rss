# Raindrop RSS bridge

Status: core implemented; both proposed defaults below are adopted. Live deployment checks remain pending.
Date: 2026-09-08 (implementation verified 2026-09-09)

## Goal and design principle

Send new articles from about 20 RSS feeds to Diego's Raindrop account hourly. Run on the always-on Geekom IT13 Max (Intel, Omarchy Linux) in Docker. Use one RSS Inbox collection and one tag per source. Send failure alerts by running `tg_command`. The default command is the host program `tg`, which posts one Telegram message. A missing command does not stop ingestion.

Principle: 90% of the features with 10% of the complexity. One Python process serving a minimal web page and running ingestion, a small settings file, an editable feed list, one SQLite file, and one Docker Compose service. No Feedly, IFTTT, paid automation service, external database, or separate scheduler. This is bookmark ingestion, not an implementation of read/unread tracking or full-text scraping.

## Confirmed choices and remaining details

Confirmed: Docker on the Geekom; hourly refresh; shared RSS Inbox with source tags; import only articles published after a feed is added; failure notifications through `tg`; an always-available minimal feed GUI and manually editable feed file; simplicity takes priority over extra features.

Adopted defaults (implemented; see the [plan](../plans/20260908-raindrop-rss-01-implementation.md) for evidence):

- Skip and report entries without a usable publication date, preserving the strict publication cutoff. Reported as a per-feed count, not one message per article.
- Run alerts through a host command, not a script shipped in this repository. Do not assume that command is installed. A missing or failing command logs a warning and leaves ingestion running; the alert is retried on a later pass.
  Resolved: `tg_command` is an argument array. Compose does not mount a notifier. A gitignored override can mount the host `tg` and the files it reads.

Python is the implementation default. Real feeds, the destination collection ID, and credentials are setup inputs; they are not needed to implement fixture-based tests.

## Configuration and operation

Use a small TOML settings file for the destination collection ID and GUI port (default `38471`). Keep subscriptions in a separate UTF-8 `feeds.json`: an array of objects with stable `id`, `url`, and `tag` fields. This file is the source of truth for subscriptions and is already a portable export; it can be copied or edited by hand. Leave OPML import out of version 1. IDs preserve history if the URL or tag is edited. Collection IDs refer to an existing collection; invalid destinations produce an error rather than sending to Unsorted.

```json
[
  { "id": "example", "url": "https://example.com/feed.xml", "tag": "example" }
]
```

Read and validate the feed file at startup and before each hourly pass; GUI additions take effect immediately. Invalid edits retain the last valid list and show a configuration error in the GUI and logs; an invalid file at startup leaves ingestion paused while the GUI remains available to show the error. The first successful load of a new feed records its UTC `added_at` in SQLite before any network fetch. That is the precise meaning of “adding the feed,” even if its publisher is temporarily unavailable. Keep this timestamp across restart, removal/re-addition of the same ID, and config changes. Removing a feed stops future polling while preserving already queued deliveries.

## Minimal feed GUI

Normal service mode always listens on the configured dedicated HTTP port, including between ingestion passes. Serve one simple HTML page from the same Python process; keep HTTP requests responsive during network fetches. Use server-rendered HTML and a normal form, with a short automatic page refresh to show progress.

List feeds in file order. Each row shows the source tag, feed URL, a health symbol with a text label, last attempted check, last successful fetch/parse, and latest publication time observed (or “never” / “none”). Health means publisher availability: `pending` before the first check, `✓ healthy` after a successful fetch/parse, `⚠ error` after a failed check with a short sanitized reason, and `✕ gone` for HTTP 404/410. Keep retrying failed/gone feeds hourly and clear the error on recovery. An inactive feed with no recent articles is not automatically dead. Show pending-delivery and needs-review counts separately so a healthy fetch cannot imply successful Raindrop delivery.

Put an **Add feed** button after the list. It opens a small form for URL and source tag; generate the stable ID. Validate HTTP(S) URLs and reject duplicate URLs/IDs, but allow an unreachable publisher to be added and report its health on polling. Save to the same `feeds.json` with an atomic replacement, then register the cutoff and schedule its first check. Re-read and validate the current file before saving; reject a conflicting edit rather than knowingly overwriting it. Report write errors without claiming success. Manual edits should also use atomic file replacement; editing while the service is stopped remains supported.

SQLite owns cutoffs, fetch/check timestamps, latest observed publication time, last fetch error, download/processing status, delivery history, and notification state. Do not write runtime status into the feed file. Keep version 1 to this list and add form; edit/remove feeds in the file.

Start a pass immediately, then run hourly in a sequential loop. A long pass delays the next pass rather than overlapping. Use finite request timeouts and a bounded pass duration; persist unfinished work for the next pass. One running container owns the database. A filesystem lock rejects a second writer or manual concurrent run.

Expose normal service mode and `--once --dry-run` for preview. Dry-run reads existing state, uses an in-memory cutoff for new feeds, and makes no changes to either production state or Raindrop. Dry-run does not start the HTTP listener. The GUI reads persisted status; no separate status service is needed.

## Fetch, filter, save

1. Fetch each RSS/Atom feed with bounded timeouts and response size. A broken feed does not stop the others. At this scale, fetch normally each hour; conditional HTTP caching is deferred.
2. Resolve an HTTP(S) article URL and parse the entry's publication date into UTC. Accept entries strictly newer than the feed's stored `added_at`. Use publication fields, not update time, to avoid importing old articles merely edited later. Under the proposed default, missing or invalid dates are skipped with an aggregate warning. Implausible future dates are not imported before their timestamp becomes current.
3. Deduplicate by article URL across all feeds managed by this installation. Lowercase scheme/hostname and remove fragments; preserve path case and query parameters. URL variants can therefore still be separate bookmarks. First eligible occurrence wins destination metadata and tags.
4. Persist eligible entries in SQLite before sending them. Queue rows retain URL, title, tag, and destination so feed rollover cannot erase an unsent article.
5. Create each bookmark through Raindrop's API. Send URL, available plain-text title, collection, source tag, and optionally a short plain-text summary. Use import time as bookmark creation time. Record the returned bookmark ID after success.

Saved URLs remain recorded indefinitely. Editing, moving, or deleting an imported bookmark in Raindrop does not cause it to be reimported. Existing bookmarks outside the bridge's local history are not scanned. Removing feeds retains their cutoff and delivery history.

## Small, explicit failure model

SQLite stores feed cutoffs and observed health/status, a delivery table, and notification incident state. Delivery states are `pending`, `sending`, `saved`, and `needs_review`.

Write `sending` before submitting a bookmark. A confirmed success becomes `saved`. A failure known to have happened before submission, or a clear temporary rejection such as a rate limit, stays pending until the next hourly pass; respect a longer server Retry-After if supplied. Avoid a separate retry engine.

A timeout after possible submission, an ambiguous server response, or a process crash with a `sending` row becomes `needs_review`. Notify the operator and do not automatically resend it. Document a small manual recovery procedure: inspect Raindrop for the exact URL, then mark the row saved with its ID or explicitly requeue it. This trades automatic recovery for lower complexity and avoids blind duplicate writes. It does not promise exactly-once delivery.

Invalid article payloads also need review. Invalid credentials or destination halt remote writes for that pass, leaving queued work intact. Publisher outages affect only their feed. A queue failure must not be reported as a successful import.

## Notifications, credentials, and deployment

Call `tg` using an argument array with the message as one argument, not shell interpolation. `tg` posts that text to Telegram with the bot token for the named profile. Settle the command packaging before deployment. Give the notification subprocess a timeout. An alert failure is logged and retried next pass without blocking article delivery.

Send one aggregate message when a new failure incident occurs and one when that incident resolves. Persist incident state so restarts do not spam messages. A continuing unchanged incident stays quiet. Report missing-date skips as counts when they first occur, not one message per article. Routine successful passes log a summary without sending Telegram messages.

Use a personal Raindrop test token, which the official API supports for access to one's own account. Mount credentials/settings read-only, and the feed-file directory and SQLite data read-write (mount the directory so atomic feed-file replacement works); keep credentials out of the image and repository. Logs contain counts and sanitized errors, with tokens and secret URL query values redacted.

Compose uses `restart: unless-stopped`, a persistent data directory, and bounded Docker log rotation. Build for Linux amd64, the Geekom's architecture; the development Mac is arm64. Docker must start on host boot. Publish the dedicated GUI port on host loopback. Remote access is an SSH tunnel, or, on the Geekom, a Cloudflare Tunnel (`tunnel` Compose profile) whose public hostname is gated by Cloudflare Access and whose origin is `http://raindrop-rss:38471` on the Compose network. A different unused host port can be configured. Back up the settings, feed file, and database while the container is stopped; restoring them together preserves cutoff and duplicate history.

The script can report encountered failures, but cannot alert if the Geekom or Docker is completely down. External uptime monitoring is outside version 1. Publishers can remove entries between polls, so hourly ingestion cannot guarantee every published article. Raindrop controls article rendering and archival.

## Acceptance criteria

- The GUI remains available between and during passes on its configured port, shows persisted feed health/timestamps and delivery counts, and places Add feed below the list. Errors/gone states recover after a successful poll; quiet feeds remain healthy.
- GUI additions persist in `feeds.json` and survive restart; manual edits load next pass. Invalid files, duplicate additions, conflicting edits, and failed writes are reported without losing the last valid list or history. Copying the file exports subscriptions without runtime status.
- Twenty fixture feeds deliver eligible articles to one collection with the correct source tags; a repeated run creates none again.
- Cutoffs survive restart and a failed initial fetch. Older, equal-cutoff, and updated-old articles are skipped; a later-published article is imported.
- Missing dates obey the selected policy. RSS and Atom, relative URLs, malformed entries, and cross-feed duplicate URLs have fixtures.
- Pending work survives restart and feed rollover. Ambiguous submissions enter review and are never blindly resent.
- A bad feed does not block healthy feeds; authentication failures, rate limits, and invalid destinations keep work visible and durable.
- Dry-run changes neither state nor Raindrop. A second writer is rejected. Secrets are absent from logs and tracked files.
- Failure/recovery notifications are delivered through `tg` without repeated unchanged alerts.
- The Geekom smoke test confirms one live import, a duplicate-free rerun, an hourly pass, and restart after reboot with state intact.

## Sources and authority

Checked 2026-09-08:

- [Personal account token](https://developer.raindrop.io/v1/authentication/token)
- [Bookmark fields](https://developer.raindrop.io/v1/raindrops)
- [Create bookmark](https://developer.raindrop.io/v1/raindrops/single)
- [Docker restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/)

This file owns intended behavior. The [implementation plan](../plans/20260908-raindrop-rss-01-implementation.md) owns execution steps and [TODO](../plans/TODO.md) owns work status. The bridge is implemented and covered by a deterministic suite; the Geekom live smoke test in the last acceptance criterion has not been performed.
