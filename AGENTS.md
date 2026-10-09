# Raindrop RSS

Human setup is `README.md`. This file is the behavior that is easy to get wrong.

## Layout

- `config/settings.toml` holds the collection id, paths, port, and `tg_command`. It is gitignored. The committed example is `config/settings.example.toml`.
- `config/raindrop-token` is one line, mode 600, gitignored.
- `subscriptions/feeds.json` is the subscription list. The filled-in file is gitignored. The example is committed.
- `data/state.db` holds cutoffs, feed health, and the delivery queue. `data/writer.lock` is the exclusive writer lock.
- Compose publishes `127.0.0.1:38471` to container port 38471. Inside the container, `bind` stays `0.0.0.0`. The image is `linux/amd64`. Logs rotate at 5 × 10 MB. Restart policy is `unless-stopped`.
- The `tunnel` profile runs `cloudflared`. It starts only when `.env` sets `COMPOSE_PROFILES=tunnel`. The Cloudflare hostname origin is `http://raindrop-rss:38471` on the Compose network, not the host port. `CLOUDFLARE_TUNNEL_TOKEN` stays in `.env`. The page has no login, so that hostname needs a Cloudflare Access application.

## Import rule

Save an entry only when its publication time is strictly after that feed's `added_at` and not after the current time. Ignore Atom `updated`. Skip entries with no usable publication date, and report the count once per incident. A future-dated entry can be saved on a later pass once its timestamp is current, if it is still in the feed.

Dedup is local and permanent, by URL: lowercase the scheme and host, drop the fragment, keep path case and the query string. The bridge does not search Raindrop. Deleting a bookmark there does not cause another save. A URL saved outside the bridge can still be imported once. Restoring an older `data/` imports URLs that database does not contain. Back up `config/`, `subscriptions/`, and `data/` together, with the container stopped.

The first successful load of a feed id records `added_at` before any network fetch. That id keeps the cutoff across restart and removal. A new id gets a new cutoff. Generated ids never reuse one the database has seen, so a second `news` feed becomes `news-2`.

## Delivery

`pending` retries on a later pass. That covers failure before the request was sent, a rate limit (honor `Retry-After` even when it is longer than an hour), rejected credentials, and a missing collection. Bad credentials or a bad destination stop remote writes for that pass. The queue stays.

`needs_review` is never resent on its own. That covers a timeout after the request may have arrived, an unreadable response, and a crash while the row was `sending`. On startup, a leftover `sending` row becomes `needs_review`.

Stop the service before `review`. A second writer on the same data directory exits 3.

```sh
docker compose stop
docker compose run --rm raindrop-rss review list
docker compose run --rm raindrop-rss review saved <row> <bookmark-id>
docker compose run --rm raindrop-rss review requeue <row>
docker compose start
```

`review requeue` sends the URL again. If Raindrop already has the bookmark, that creates a duplicate.

`--once --dry-run` copies state into memory, writes nothing, opens no port, and does not call Raindrop or Telegram. It cannot be combined with `review`. Global options come before the subcommand. `--debug` raises this program's log level only. Leave urllib3 request logging off: feed URLs can carry secrets in the query.

## Feeds file

An invalid file keeps the last valid list, shows the error on the page, and sends one Telegram alert. Queued deliveries continue. If no valid list has ever loaded, ingestion pauses, including remote writes, and the page stays up. Additions from the form are atomic and refuse to overwrite a file that changed after the form was opened. Hand edits load at the next pass. Write the file by atomic replace. Removing a feed stops polling and keeps its cutoff and queued rows.

## Telegram

`tg_command` is an argv. The alert text is one extra argument, never a shell string. The default is `tg --`. `tg` is a host command, not a file in this repository; mount it, and any secret files it reads, from a gitignored `compose.override.yaml`. A missing binary, a non-zero exit, or a timeout is logged and the pass still saves articles. The incident list is updated only after a successful alert, so the next pass retries. One message when an incident starts, one when it clears. An unchanged incident stays quiet.

## Do not commit

`.env`, `dockertoken`, `config/settings.toml`, `config/raindrop-token`, `subscriptions/feeds.json`, `data/`, `rss-import-app.json`, and Reeder exports.
