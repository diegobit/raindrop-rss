"""One server-rendered status page with an Add feed form. Nothing else."""

import html
import logging
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .config import ConfigError, display_url, normalize_url

log = logging.getLogger(__name__)

FORM_LIMIT = 4096
SOCKET_TIMEOUT = 15
REFRESH_SECONDS = 30

HEALTH = {
    'pending': ('&#9675;', 'pending'),
    'healthy': ('&#10003;', 'healthy'),
    'error': ('&#9888;', 'error'),
    'gone': ('&#10005;', 'gone'),
}

STYLE = '''
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 60rem; line-height: 1.4; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8884; vertical-align: top; }
th { font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; opacity: .7; }
td.gone, td.error { font-weight: 600; }
.counts span { display: inline-block; margin-right: 1.5rem; }
.notice { padding: .6rem .8rem; border-left: 4px solid currentColor; margin: 1rem 0; }
.bad { color: #b3261e; }
.good { color: #1b6b3a; }
form { margin-top: 1rem; display: grid; gap: .5rem; max-width: 32rem; }
input { padding: .4rem; font: inherit; }
button { padding: .5rem 1rem; font: inherit; width: fit-content; }
small { opacity: .7; }
'''


def escape(value):
    return html.escape('' if value is None else str(value), quote=True)


def moment(value):
    if not value:
        return 'never'
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


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
        parts.append('<table><thead><tr><th>Tag</th><th>Feed</th><th>Health</th>'
                     '<th>Last check</th><th>Last success</th><th>Latest article</th></tr></thead><tbody>')
        for feed in feeds:
            row = rows.get(feed['id']) or {}
            health = row.get('health', 'pending')
            symbol, label = HEALTH.get(health, HEALTH['pending'])
            detail = f'<br><small>{escape(row.get("error"))}</small>' if row.get('error') else ''
            parts.append(
                f'<tr><td>{escape(feed["tag"])}</td>'
                f'<td>{escape(display_url(feed["url"]))}<br><small>{escape(feed["id"])}</small></td>'
                f'<td class="{escape(health)}">{symbol} {escape(label)}{detail}</td>'
                f'<td>{escape(moment(row.get("attempted_at")))}</td>'
                f'<td>{escape(moment(row.get("success_at")))}</td>'
                f'<td>{escape(moment(row.get("latest_at")) if row.get("latest_at") else "none")}</td></tr>')
        if not feeds:
            parts.append('<tr><td colspan="6">No subscriptions yet.</td></tr>')
        parts.append('</tbody></table>')
        # The button sits below the list and opens the form on its own page, so
        # the periodic refresh here cannot wipe out half-typed input.
        parts.append(
            '<p><a href="/add"><button type="button">Add feed</button></a></p>'
            '<p><small>Edit or remove feeds directly in feeds.json; changes load on the next pass.</small></p>'
            '</body></html>')
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
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'")
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
            else:
                self.reply(404, b'<!doctype html><p>Not found.')
        except sqlite3.Error:
            self.fail_safely()

    def do_POST(self):
        if self.path != '/add':
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
        try:
            message, bad, url, tag = self.server.page.add(form)
            self.reply(200, self.server.page.form(message, bad, url, tag))
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
