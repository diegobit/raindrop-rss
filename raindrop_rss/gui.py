"""One server-rendered status page, with add and confirmed removal."""

import html
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import ConfigError, display_url, normalize_url

log = logging.getLogger(__name__)

FORM_LIMIT = 4096
SOCKET_TIMEOUT = 15
REFRESH_SECONDS = 30

HEALTH = {
    'pending': ('pending', '<circle cx="8" cy="8" r="4.25" fill="none" stroke="currentColor" stroke-width="1.6"/>'),
    'healthy': ('healthy', '<path d="M3.8 8.4 6.6 11.1 12.2 4.8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'),
    'error': ('error', '<path d="M8 2.8 14.4 14H1.6L8 2.8Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M8 6.4v3.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>'),
    'gone': ('gone', '<path d="M4.4 4.4 11.6 11.6M11.6 4.4 4.4 11.6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'),
}

STYLE = '''
:root {
  color-scheme: light dark;
  --ink: light-dark(#1c1b19, #f4f1ea);
  --muted: light-dark(#6e6a62, #a8a49c);
  --line: light-dark(#e4e0d8, #2e2d2a);
  --paper: light-dark(#f6f4ef, #141413);
  --chip: light-dark(#fff, #22211e);
  --ok: light-dark(#0f7a38, #8ed7a6);
  --warn: light-dark(#9a4b10, #f0b27a);
  --bad: light-dark(#a32632, #f3a3a8);
}
* { box-sizing: border-box; }
body {
  font-family: system-ui, sans-serif; margin: 0 auto; max-width: 68rem;
  padding: 2rem 1.25rem 3rem; line-height: 1.45; background: var(--paper); color: var(--ink);
}
h1 { font-size: 1.35rem; font-weight: 650; letter-spacing: -.02em; margin: 0 0 .4rem; }
h2 { font-size: 1.15rem; font-weight: 650; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .75rem .5rem; border-bottom: 1px solid var(--line); vertical-align: middle; }
th { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 600; }
.counts span { display: inline-block; margin: 0 1.4rem 0 0; }
.notice { padding: .6rem .8rem; border-left: 4px solid currentColor; margin: 1rem 0; }
.bad { color: var(--bad); }
.good { color: var(--ok); }
.feed { display: flex; gap: .7rem; align-items: center; }
.mark {
  position: relative; display: inline-grid; width: 1.4rem; height: 1.4rem; flex: none;
  place-items: center; border-radius: .35rem; background: light-dark(#e8e4db, #2a2926);
  color: var(--muted); font-size: .72rem; font-weight: 700;
}
.ico { position: absolute; inset: 0; border-radius: inherit; background: center / cover no-repeat; }
.name { font-weight: 620; }
.url, .id { color: var(--muted); font-size: .82rem; }
.url { word-break: break-all; }
.badge { white-space: nowrap; display: inline-flex; align-items: center; gap: .35rem; font-weight: 620; }
.glyph { width: 1rem; height: 1rem; flex: none; }
.healthy .badge { color: var(--ok); }
.error .badge { color: var(--warn); }
.gone .badge { color: var(--bad); }
.pending .badge { color: var(--muted); font-weight: 520; }
.reason { display: block; margin-top: .25rem; color: var(--muted); font-weight: 450; }
.actions a { color: var(--muted); text-decoration: none; }
.actions a:hover { color: var(--bad); }
.hint { color: var(--muted); font-size: .9rem; }
.foot { margin: .4rem 0 0; }
a.button, button {
  display: inline-block; padding: .5rem 1rem; border-radius: 999px; border: 1px solid var(--line);
  background: var(--chip); color: inherit; text-decoration: none; font: inherit; cursor: pointer;
}
form { margin-top: 1rem; display: grid; gap: .6rem; max-width: 32rem; }
input { padding: .45rem .55rem; font: inherit; border: 1px solid var(--line); border-radius: .4rem; background: var(--chip); color: inherit; }
'''


def escape(value):
    return html.escape('' if value is None else str(value), quote=True)


def moment(value):
    if not value:
        return 'never'
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def site_icon(url):
    """Favicon for the feed's host. Query strings stay out of the page."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    return f'{parts.scheme}://{parts.hostname}/favicon.ico'


def initial(tag):
    for char in tag:
        if char.isalnum():
            return char.upper()
    return '·'


def glyph(health):
    label, drawing = HEALTH.get(health, HEALTH['pending'])
    return (f'<svg class="glyph" viewBox="0 0 16 16" aria-hidden="true">{drawing}</svg>', label)


class Page:
    """Renders the page from persisted state, so no status service is needed."""

    def __init__(self, bridge):
        self.bridge = bridge
        self.token = secrets.token_urlsafe(32)

    def snapshot(self):
        with self.bridge.guard:
            feeds, revision = list(self.bridge.feeds), self.bridge.revision
            config_error = self.bridge.config_error
        return feeds, revision, config_error, self.bridge.state.feed_map()

    def head(self, refresh):
        return [
            '<!doctype html><html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f'<meta http-equiv="refresh" content="{REFRESH_SECONDS};url=/">' if refresh else '',
            '<title>Raindrop RSS</title><style>', STYLE, '</style></head><body>',
            '<h1>Raindrop RSS</h1>',
        ]

    def render(self, message=None, bad=False):
        """The status page. It refreshes itself, so it carries no input fields."""
        feeds, _, config_error, rows = self.snapshot()
        state = self.bridge.state
        counts = state.counts()
        parts = self.head(refresh=True)
        if message:
            parts.append(f'<p class="notice {"bad" if bad else "good"}">{escape(message)}</p>')
        if config_error:
            parts.append('<p class="notice bad">Feed configuration error, using the last valid list: '
                         f'{escape(config_error)}</p>')
        if remote := state.get_meta('remote_error'):
            parts.append(f'<p class="notice bad">Raindrop: {escape(remote)}</p>')
        parts.append(
            '<p class="counts">'
            f'<span>Pending delivery: <b>{counts["pending"] + counts["sending"]}</b></span>'
            f'<span>Needs review: <b>{counts["needs_review"]}</b></span>'
            f'<span>Saved: <b>{counts["saved"]}</b></span></p>'
            f'<p><small>{"Pass running." if state.get_meta("pass_running") else "Idle."} '
            f'Last pass {escape(moment(state.get_meta("last_pass_at")))}.</small></p>')
        parts.append('<table><thead><tr><th>Feed</th><th>Health</th>'
                     '<th>Last check</th><th>Last success</th><th>Latest article</th>'
                     '<th></th></tr></thead><tbody>')
        for feed in feeds:
            row = rows.get(feed['id']) or {}
            health = row.get('health', 'pending')
            symbol, label = glyph(health)
            reason = (f'<span class="reason">{escape(row["error"])}</span>'
                      if row.get('error') else '')
            icon = site_icon(feed['url'])
            cover = f' style="background-image:url(\'{escape(icon)}\')"' if icon else ''
            parts.append(
                '<tr><td><div class="feed">'
                f'<span class="mark">{escape(initial(feed["tag"]))}'
                f'<span class="ico"{cover}></span></span><div>'
                f'<div class="name">{escape(feed["tag"])}</div>'
                f'<div class="url">{escape(display_url(feed["url"]))}</div>'
                f'<div class="id">{escape(feed["id"])}</div></div></div></td>'
                f'<td class="health {escape(health)}"><span class="badge">{symbol} {escape(label)}</span>{reason}</td>'
                f'<td>{escape(moment(row.get("attempted_at")))}</td>'
                f'<td>{escape(moment(row.get("success_at")))}</td>'
                f'<td>{escape(moment(row.get("latest_at")) if row.get("latest_at") else "none")}</td>'
                f'<td class="actions"><a href="/remove?id={escape(feed["id"])}">Remove</a></td></tr>')
        if not feeds:
            parts.append('<tr><td colspan="6">No subscriptions yet.</td></tr>')
        parts.append('</tbody></table>')
        # Add sits on its own page, below the list, so this refresh cannot
        # wipe out half-typed input. Remove is a link for the same reason.
        parts.append(
            '<p class="hint">Edits in feeds.json apply on the next pass.</p>'
            '<p class="foot"><a class="button" href="/add">Add feed</a></p></body></html>')
        return ''.join(parts).encode('utf-8')

    def form(self, message=None, bad=False, url='', tag=''):
        """The add-feed page. No auto-refresh: the reader is typing."""
        with self.bridge.guard:
            revision = self.bridge.revision
        parts = self.head(refresh=False)
        if message:
            parts.append(f'<p class="notice {"bad" if bad else "good"}">{escape(message)}</p>')
        parts.append(
            '<h2>Add feed</h2><form method="post" action="/add">'
            f'<input type="hidden" name="token" value="{escape(self.token)}">'
            f'<input type="hidden" name="revision" value="{escape(revision)}">'
            '<label>Feed URL<br><input name="url" type="url" required maxlength="2000" '
            f'placeholder="https://example.com/feed.xml" size="50" value="{escape(url)}"></label>'
            '<label>Source tag<br><input name="tag" required maxlength="100" placeholder="example" '
            f'value="{escape(tag)}"></label>'
            '<button type="submit">Add feed</button></form>'
            '<p><a href="/">Back to the feed list</a></p></body></html>')
        return ''.join(parts).encode('utf-8')

    def confirm_remove(self, ident, message=None, bad=False):
        """Ask before a feed stops being polled. No auto-refresh."""
        feeds, revision, _, _ = self.snapshot()
        feed = next((item for item in feeds if item['id'] == ident), None)
        parts = self.head(refresh=False)
        if message:
            parts.append(f'<p class="notice {"bad" if bad else "good"}">{escape(message)}</p>')
        if feed is None:
            parts.append('<h2>Feed not found</h2><p>That subscription is not in the list.</p>'
                         '<p><a href="/">Back to the feed list</a></p></body></html>')
            return ''.join(parts).encode('utf-8')
        parts.append(
            '<h2>Remove this feed?</h2>'
            f'<p class="name">{escape(feed["tag"])}</p>'
            f'<p class="url">{escape(display_url(feed["url"]))}</p>'
            '<p>Polling stops. Articles already saved stay in Raindrop, and this feed\'s '
            'history stays recorded.</p>'
            '<form method="post" action="/remove">'
            f'<input type="hidden" name="token" value="{escape(self.token)}">'
            f'<input type="hidden" name="revision" value="{escape(revision)}">'
            f'<input type="hidden" name="id" value="{escape(feed["id"])}">'
            '<button type="submit">Remove feed</button></form>'
            '<p><a href="/">Cancel</a></p></body></html>')
        return ''.join(parts).encode('utf-8')

    def valid_token(self, supplied):
        # compare_digest refuses non-ASCII strings; a forged token must be
        # rejected, not turned into a 500.
        try:
            return secrets.compare_digest(supplied, self.token)
        except TypeError:
            return False

    def add(self, form):
        """Returns (message, failed, url, tag). Never claims unconfirmed success."""
        url, tag = form.get('url', [''])[0].strip(), form.get('tag', [''])[0].strip()
        if not self.valid_token(form.get('token', [''])[0]):
            return 'Form token rejected. Reload the page and try again.', True, url, tag
        try:
            normalized = normalize_url(url)
            if not tag:
                raise ConfigError('A source tag is required')
            self.bridge.add(form.get('revision', [''])[0], normalized, tag)
        except ConfigError as exc:
            return str(exc), True, url, tag
        except sqlite3.Error:
            log.exception('Could not register the new feed')
            return ('Saved to feeds.json, but recording its cutoff failed. '
                    'Check the database before the next pass.'), True, '', ''
        return f'Added {display_url(normalized)}; its first check is scheduled now.', False, '', ''

    def remove(self, form):
        """Returns (message, failed). Never claims unconfirmed success."""
        ident = form.get('id', [''])[0]
        if not self.valid_token(form.get('token', [''])[0]):
            return 'Form token rejected. Reload the page and try again.', True
        try:
            self.bridge.remove(form.get('revision', [''])[0], ident)
        except ConfigError as exc:
            return str(exc), True
        return 'Removed. Polling for that feed has stopped.', False


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = SOCKET_TIMEOUT
    server_version = 'raindrop-rss'
    sys_version = ''

    def log_message(self, fmt, *args):
        # Never echo the request line: a path or query could carry a secret.
        log.debug('gui request handled')

    def log_error(self, fmt, *args):
        log.debug('gui request rejected')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'none'; style-src 'unsafe-inline'; img-src http: https:")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        page = self.server.page
        try:
            if path == '/':
                self.reply(200, page.render())
            elif path == '/add':
                self.reply(200, page.form())
            elif path == '/remove':
                ident = parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '').get('id', [''])[0]
                self.reply(200, page.confirm_remove(ident))
            else:
                self.reply(404, b'<!doctype html><p>Not found.')
        except sqlite3.Error:
            self.fail_safely()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path not in ('/add', '/remove'):
            self.reply(404, b'<!doctype html><p>Not found.')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = -1
        if not 0 <= length <= FORM_LIMIT:
            self.reply(413, b'<!doctype html><p>Form too large.')
            return
        body = self.rfile.read(length)
        form = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        page = self.server.page
        try:
            if path == '/add':
                message, bad, url, tag = page.add(form)
                self.reply(200, page.form(message, bad, url, tag))
            else:
                message, bad = page.remove(form)
                self.reply(200, page.confirm_remove(form.get('id', [''])[0], message, bad) if bad
                           else page.render(message))
        except sqlite3.Error:
            self.fail_safely()

    def fail_safely(self):
        log.exception('Database error while serving the page')
        self.reply(500, b'<!doctype html><p>The database is unavailable. Check the service logs.')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bridge, settings):
        super().__init__((settings.bind, settings.port), Handler)
        self.socket.settimeout(SOCKET_TIMEOUT)
        self.page = Page(bridge)


def serve(bridge, settings):
    """Start the page in a background thread; ingestion keeps the main thread."""
    server = Server(bridge, settings)
    thread = threading.Thread(target=server.serve_forever, name='gui', daemon=True)
    thread.start()
    log.info('GUI listening on http://%s:%d/', settings.bind, settings.port)
    return server
