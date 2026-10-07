# Raindrop RSS

Sites you follow keep publishing. The articles you have not seen yet should become Raindrop bookmarks, in one collection, tagged by where they came from. The posts already sitting in those feeds should stay where they are.

Raindrop RSS checks the feeds once an hour. It is one program: it fetches the feeds, saves the new articles, and serves a status page on this machine. Subscriptions are a JSON file you can edit. What it has already saved lives in a SQLite file beside them. When a feed breaks, or a save to Raindrop is uncertain, it runs the command in `tg_command` and passes the message as the last argument. If that command is missing or fails, the articles are still saved and the alert is tried again on the next pass.

## Install

You need Docker, a Raindrop personal test token, and a collection that already exists. Create the token at <https://app.raindrop.io/settings/integrations>: make an app, then copy its test token.

```sh
cp config/settings.example.toml config/settings.toml
cp subscriptions/feeds.example.json subscriptions/feeds.json
mkdir -p data
printf '%s\n' 'YOUR_TEST_TOKEN' > config/raindrop-token
chmod 600 config/raindrop-token
printf 'RSS_UID=%s\nRSS_GID=%s\n' "$(id -u)" "$(id -g)" > .env
```

In `config/settings.toml`, set `collection_id` to the number at the end of the collection's Raindrop URL. List each feed in `subscriptions/feeds.json`:

```json
[
  { "id": "example", "url": "https://example.com/feed.xml", "tag": "example" }
]
```

The `id` is the feed's identity. Changing the URL or the tag keeps its history. A new `id` starts over.

Alerts are optional. `tg` is a host command that posts one message to Telegram; this repository does not include it. Mount the command, and any files it reads, in a `compose.override.yaml` you do not commit:

```yaml
services:
  raindrop-rss:
    volumes:
      - /usr/local/bin/tg:/usr/local/bin/tg:ro
```

Without that mount the service still runs. To publish the page on a different host port, set `RSS_PORT` in `.env`.

```sh
docker compose up -d --build
```

The page is at <http://127.0.0.1:38471/>. From another machine:

```sh
ssh -L 38471:127.0.0.1:38471 diego@geekom
```

Enable Docker at boot (`systemctl enable docker`) so the hourly check returns after a restart.

To work on the code:

```sh
uv venv --python 3.14
uv pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .
```

## Using it

The status page lists each feed with its icon, address, and health. Healthy means the feed downloaded and parsed, and that state is shown in green. The counts above the list are bookmarks waiting to be saved, bookmarks already saved, and bookmarks that need a look. The page refreshes every 30 seconds. **Add feed** is at the bottom and opens its own form, so the refresh cannot clear what you type. **Remove** asks you to confirm; polling stops, and articles already saved stay saved. Edits you make directly in `feeds.json` apply on the next hourly pass.

An article is saved when its publication date is later than the moment you added its feed, and not in the future. A later edit of an old post does not make it new. Each article URL is saved once.

Stop the container before copying `config/`, `subscriptions/`, and `data/`. Restore those three together.
