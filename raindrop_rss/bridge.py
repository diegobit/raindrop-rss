"""The sequential ingestion pass: fetch, filter, queue, deliver, alert."""

import calendar
import html
import logging
import re
import threading
import time
from urllib.parse import urljoin

import feedparser

from .adapters import HTTP, Raindrop, deadline, notify
from .config import ConfigError, add_feed, display_url, normalize_url, read_feeds, remove_feed

log = logging.getLogger(__name__)

# feedparser flags these while still parsing the document correctly: a missing or
# non-XML Content-Type, and a declared encoding it had to override.
ADVISORY = (feedparser.NonXMLContentType, feedparser.CharacterEncodingOverride)

# Share of the pass budget spent fetching, leaving the rest for delivery.
FETCH_SHARE = 0.65
MESSAGE_LIMIT = 3500


def plain_text(value):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value))).strip()[:1000]


def publication(entry):
    """Publication time in UTC, or None.

    Only a real publication field counts. feedparser aliases a missing `updated`
    to `published` but never the reverse, so Atom `updated` cannot make an old
    article eligible merely because it was edited.
    """
    if 'published' not in entry or 'published_parsed' not in entry:
        return None
    try:
        return calendar.timegm(entry['published_parsed'])
    except (TypeError, ValueError, OverflowError):
        return None


def parse_articles(body, base_url, feed, cutoff, now, destination):
    """Return (queue rows, entries without a usable date, latest date observed)."""
    parsed = feedparser.parse(body, response_headers={'content-location': base_url})
    if not parsed.get('version'):
        raise ValueError('Unrecognised RSS/Atom document')
    problem = parsed.get('bozo_exception')
    if problem is not None and not isinstance(problem, ADVISORY) and not parsed.get('entries'):
        raise ValueError('RSS/Atom document could not be parsed')
    articles, missing, latest = [], 0, None
    for entry in parsed.entries:
        published = publication(entry)
        if published is None:
            missing += 1
            continue
        latest = published if latest is None else max(latest, published)
        # Strictly newer than the cutoff, and not dated in the future.
        if published <= cutoff or published > now:
            continue
        link = entry.get('link')
        if not link:
            continue
        try:
            url = normalize_url(urljoin(base_url, link))
        except (ConfigError, ValueError, TypeError):
            continue
        articles.append((url, plain_text(entry.get('title', '')), feed['tag'], destination, feed['id']))
    return articles, missing, latest


class Bridge:
    def __init__(self, settings, state, dry_run=False, http=None):
        self.settings, self.state, self.dry_run = settings, state, dry_run
        self.http = http or HTTP()
        self.guard = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.feeds = []
        self.loaded = False
        self.config_error = None
        self.revision = ''
        self.reload()

    # -- subscriptions ----------------------------------------------------

    def reload(self):
        """Re-read feeds.json, keeping the last valid list if the file is broken."""
        with self.guard:
            try:
                feeds, revision = read_feeds(self.settings.feeds_file)
            except ConfigError as exc:
                self.config_error = str(exc)
                log.warning('Feed configuration invalid; retaining last valid subscriptions')
                return False
            self.state.register(feeds, time.time())
            self.feeds, self.revision, self.config_error = feeds, revision, None
            self.loaded = True
            return True

    def add(self, revision, url, tag):
        """Save, register the cutoff, and schedule the first check immediately."""
        with self.guard:
            add_feed(self.settings.feeds_file, revision, url, tag, self.state.known_feed_ids())
            if not self.reload():
                raise ConfigError('Feed file saved but could not be reloaded; see the configuration error')
        self.wake.set()

    def remove(self, revision, ident):
        """Stop polling this feed. Its cutoff and queued deliveries stay."""
        with self.guard:
            remove_feed(self.settings.feeds_file, revision, ident)
            if not self.reload():
                raise ConfigError('Feed file saved but could not be reloaded; see the configuration error')

    # -- one pass ---------------------------------------------------------

    def fetch(self, feed, end):
        row = self.state.feed(feed['id'])
        if row is None:
            return
        self.state.execute('UPDATE feeds SET attempted_at=? WHERE id=?', (time.time(), feed['id']))
        seconds = min(self.settings.request_timeout, max(0.001, end - time.monotonic()))
        try:
            response = self.http.request('GET', feed['url'], seconds, self.settings.max_feed_bytes,
                                         headers={'User-Agent': 'raindrop-rss/1.0'})
            if response.status != 200:
                health = 'gone' if response.status in (404, 410) else 'error'
                self.state.execute('UPDATE feeds SET health=?,error=? WHERE id=?',
                                   (health, f'Publisher HTTP {response.status}', feed['id']))
                return
            with deadline(max(0.001, min(5, end - time.monotonic()))):
                articles, missing, latest = parse_articles(
                    response.body, response.url, feed, row['added_at'], time.time(), self.settings.collection_id)
        except Exception:
            # A broken publisher marks only its own feed; the pass continues.
            log.warning('Feed check failed: %s', display_url(feed['url']))
            self.state.execute("UPDATE feeds SET health='error',error=? WHERE id=?",
                               ('Fetch or RSS/Atom parse failed', feed['id']))
            return
        # Queue first: a failed write must never be recorded as a healthy import.
        self.state.queue(articles)
        self.state.execute('''UPDATE feeds SET health='healthy',error=NULL,success_at=?,
            latest_at=CASE WHEN latest_at IS NULL OR latest_at < ? THEN ? ELSE latest_at END,
            missing_dates=? WHERE id=?''', (time.time(), latest, latest, missing, feed['id']))
        if self.dry_run:
            for article in articles:
                log.info('Preview candidate: %s', display_url(article[0]))

    def token(self):
        token = self.settings.token_file.read_text(encoding='utf-8').strip()
        if not token or any(character.isspace() for character in token):
            raise ValueError('malformed token')
        return token

    def deliver(self, end):
        if self.dry_run:
            return
        if not self.state.rows("SELECT id FROM deliveries WHERE state IN ('pending','needs_review') LIMIT 1"):
            # Nothing is outstanding, so an earlier remote failure is history.
            # Clearing it here lets the recovery alert fire without a new article.
            self.state.set_meta('remote_error', None)
            self.state.set_meta('remote_retry_at', 0)
            return
        if time.time() < self.state.get_meta('remote_retry_at', 0):
            return
        rows = self.state.rows("SELECT * FROM deliveries WHERE state='pending' AND retry_at<=? ORDER BY id",
                               (time.time(),))
        if not rows:
            return
        try:
            api = Raindrop(self.http, self.token())
        except (OSError, ValueError):
            self.state.set_meta('remote_error', 'Raindrop token file missing or invalid')
            return
        verified = set()
        for row in rows:
            if self.stop.is_set() or time.monotonic() >= end:
                break
            if row['destination'] not in verified:
                seconds = min(self.settings.request_timeout, max(0.001, end - time.monotonic()))
                check = api.check_collection(row['destination'], seconds)
                if check.state != 'ok':
                    self.halt_remote(check)
                    return
                verified.add(row['destination'])
                self.state.set_meta('remote_error', None)
            if self.stop.is_set() or time.monotonic() >= end:
                break
            self.state.set_delivery(row['id'], 'sending')
            seconds = min(self.settings.request_timeout, max(0.001, end - time.monotonic()))
            result = api.create(row, seconds)
            retry_at = max(time.time() + self.settings.interval_seconds, result.retry_at) if result.state == 'pending' else 0
            self.state.set_delivery(row['id'], result.state, result.error, result.bookmark_id, retry_at)
            if result.halt:
                self.halt_remote(result)
                return

    def halt_remote(self, result):
        """Stop remote writes for this pass; queued work stays pending and visible."""
        self.state.set_meta('remote_retry_at',
                            max(time.time() + self.settings.interval_seconds, result.retry_at))
        self.state.set_meta('remote_error', result.error)

    # -- notifications ----------------------------------------------------

    def incidents(self):
        """Current failure incidents keyed so an unchanged one stays quiet."""
        result = {}
        with self.guard:
            feeds, config_error = list(self.feeds), self.config_error
        if config_error:
            result['configuration'] = 'Feed configuration invalid: ' + config_error
        rows = self.state.feed_map()
        for feed in feeds:
            row = rows.get(feed['id'])
            if row is None:
                continue
            if row['health'] in ('gone', 'error'):
                result['feed:' + feed['id']] = 'Publisher failure: ' + display_url(feed['url'])
            if row['missing_dates']:
                result['dates:' + feed['id']] = (f"{row['missing_dates']} entries without a publication date "
                                                 f'were skipped: {display_url(feed["url"])}')
        # Keyed per row: each ambiguous delivery is its own incident, so a second
        # one is announced instead of hiding behind the first.
        for row in self.state.rows("SELECT id,url FROM deliveries WHERE state='needs_review' ORDER BY id"):
            result['review:%d' % row['id']] = (f'Delivery {row["id"]} needs manual review: '
                                               f'{display_url(row["url"])}')
        if self.state.rows("SELECT id FROM deliveries WHERE state='pending' AND error IS NOT NULL LIMIT 1"):
            result['delivery'] = 'Pending deliveries encountered an error'
        if error := self.state.get_meta('remote_error'):
            result['raindrop'] = error
        return result

    def alerts(self):
        """One aggregate message per new incident and one when it resolves."""
        if self.dry_run:
            return
        current = self.incidents()
        previous = self.state.get_meta('notified_incidents', {})
        new = current.keys() - previous.keys()
        resolved = previous.keys() - current.keys()
        if not new and not resolved:
            return
        lines = ['Raindrop RSS']
        if new:
            lines += ['Failures:'] + [current[key] for key in sorted(new)]
        if resolved:
            lines += ['Resolved:'] + [previous[key] for key in sorted(resolved)]
        message = '\n'.join(lines)[:MESSAGE_LIMIT]
        if notify(self.settings.tg_command, message, self.settings.notify_timeout):
            self.state.set_meta('notified_incidents', current)
        # Otherwise the unchanged stored state makes the next pass retry.

    # -- loop -------------------------------------------------------------

    def run_pass(self, new_only=False):
        start = time.monotonic()
        end = start + self.settings.pass_seconds
        self.reload()
        if not self.loaded:
            # No valid subscription file has ever loaded, so the destination and
            # tags are unknown. Pause everything, including queued remote writes,
            # and leave the GUI to show the error.
            log.warning('Ingestion paused: no valid feeds.json has been loaded yet')
            self.alerts()
            return self.state.counts()
        with self.guard:
            feeds = list(self.feeds)
        rows = self.state.feed_map()
        if new_only:
            feeds = [feed for feed in feeds if (rows.get(feed['id']) or {}).get('attempted_at') is None]
        else:
            # Feeds left unfinished by an earlier budget cut go first; a stable
            # sort keeps file order on the first pass.
            feeds.sort(key=lambda feed: (rows.get(feed['id']) or {}).get('attempted_at') or 0)
        fetch_end = start + self.settings.pass_seconds * FETCH_SHARE
        self.state.set_meta('pass_running', True)
        try:
            for feed in feeds:
                if self.stop.is_set() or time.monotonic() >= fetch_end:
                    log.info('Fetch budget reached; remaining feeds run next pass')
                    break
                with self.guard:
                    if feed not in self.feeds:
                        continue
                self.fetch(feed, fetch_end)
            self.deliver(end)
            self.alerts()
        finally:
            self.state.set_meta('pass_running', False)
            self.state.set_meta('last_pass_at', time.time())
        counts = self.state.counts()
        log.info('Pass finished: feeds=%d pending=%d saved=%d needs_review=%d',
                 len(feeds), counts['pending'], counts['saved'], counts['needs_review'])
        return counts

    def run(self):
        """Start immediately, then hourly. A long pass delays the next one."""
        self.run_pass()
        next_pass = time.monotonic() + self.settings.interval_seconds
        while not self.stop.is_set():
            self.wake.wait(max(0, next_pass - time.monotonic()))
            self.wake.clear()
            if self.stop.is_set():
                break
            if time.monotonic() >= next_pass:
                self.run_pass()
                next_pass = time.monotonic() + self.settings.interval_seconds
            else:
                # Woken by a GUI addition: check only the new feed, keeping the
                # hourly schedule and every other feed's retry timing intact.
                self.run_pass(new_only=True)

    def shutdown(self):
        self.stop.set()
        self.wake.set()
