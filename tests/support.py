"""Fixtures and fakes shared by the deterministic suite. No real network."""

import json
import shutil
import tempfile
import time
import unittest
from email.utils import formatdate
from pathlib import Path

import requests

from raindrop_rss.adapters import Response
from raindrop_rss.bridge import Bridge
from raindrop_rss.config import Settings
from raindrop_rss.state import State

COLLECTION = 4242
FEED_BASE = 'https://feeds.test'
API = 'https://api.raindrop.io/rest/v1'


def rfc822(when):
    return formatdate(when, usegmt=True)


def rss(items, title='Fixture'):
    body = [f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>{title}</title>']
    for item in items:
        body.append('<item>')
        if 'title' in item:
            body.append(f'<title>{item["title"]}</title>')
        if 'link' in item:
            body.append(f'<link>{item["link"]}</link>')
        if 'published' in item:
            body.append(f'<pubDate>{rfc822(item["published"])}</pubDate>')
        if 'raw_date' in item:
            body.append(f'<pubDate>{item["raw_date"]}</pubDate>')
        body.append('</item>')
    body.append('</channel></rss>')
    return ''.join(body).encode('utf-8')


def atom(items, title='Fixture'):
    body = [f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f'<title>{title}</title><id>urn:{title}</id>']
    for index, item in enumerate(items):
        body.append(f'<entry><id>urn:{title}:{index}</id>')
        if 'title' in item:
            body.append(f'<title>{item["title"]}</title>')
        if 'link' in item:
            body.append(f'<link href="{item["link"]}"/>')
        if 'published' in item:
            body.append(f'<published>{stamp(item["published"])}</published>')
        if 'updated' in item:
            body.append(f'<updated>{stamp(item["updated"])}</updated>')
        body.append('</entry>')
    body.append('</feed>')
    return ''.join(body).encode('utf-8')


def stamp(when):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(when))


class FakeHTTP:
    """Routes URLs to canned responses, callables, or exceptions."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, url, body=b'', status=200, headers=None):
        self.routes[url] = Response(status, headers or {}, body, url)

    def fail(self, url, error):
        self.routes[url] = error

    def request(self, method, url, seconds, max_bytes, **kwargs):
        self.calls.append({'method': method, 'url': url, 'seconds': seconds, **kwargs})
        handler = self.routes.get(url)
        if handler is None:
            raise requests.ConnectionError(f'no route for {url}')
        if callable(handler):
            handler = handler(method, url, kwargs)
        if isinstance(handler, BaseException):
            raise handler
        if len(handler.body) > max_bytes:
            raise ValueError('Response exceeds size limit')
        return handler


class FakeRaindrop:
    """Accepts creations into one collection and records them."""

    def __init__(self, http, collection=COLLECTION):
        self.collection = collection
        self.created = []
        self.next_id = 1000
        self.create_result = None      # override: callable(payload) -> Response
        self.collection_result = None
        http.routes[f'{API}/collection/{collection}'] = self.on_collection
        http.routes[f'{API}/raindrop'] = self.on_create

    def on_collection(self, method, url, kwargs):
        if self.collection_result is not None:
            return self.collection_result
        return json_response(200, {'result': True, 'item': {'_id': self.collection, 'access': {'level': 4}}})

    def on_create(self, method, url, kwargs):
        payload = kwargs['json']
        if self.create_result is not None:
            result = self.create_result(payload)
            if result is not None:
                return result
        self.next_id += 1
        self.created.append(payload)
        return json_response(200, {'result': True, 'item': {
            '_id': self.next_id, 'link': payload['link'], 'tags': payload['tags'],
            'collection': {'$id': payload['collection']['$id']}}})


def json_response(status, data, headers=None):
    return Response(status, headers or {}, json.dumps(data).encode('utf-8'), 'https://api.raindrop.io/x')


class Harness(unittest.TestCase):
    """A temporary installation: settings, feeds.json, token, state, bridge."""

    interval = 3600

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='raindrop-rss-test.'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / 'config').mkdir()
        (self.root / 'subscriptions').mkdir()
        self.feeds_file = self.root / 'subscriptions' / 'feeds.json'
        self.data_dir = self.root / 'data'
        self.data_dir.mkdir()
        self.token_file = self.root / 'config' / 'raindrop-token'
        self.token_file.write_text('test-token-not-a-real-secret\n')
        self.write_feeds([])
        self.settings = Settings(
            collection_id=COLLECTION, feeds_file=self.feeds_file, data_dir=self.data_dir,
            token_file=self.token_file, port=0, interval_seconds=self.interval,
            request_timeout=5, pass_seconds=30, tg_command=('true',))
        self.http = FakeHTTP()
        self.raindrop = FakeRaindrop(self.http)
        self.sent = []
        self.states = []

    # -- fixtures ---------------------------------------------------------

    def write_feeds(self, feeds):
        self.feeds_file.write_text(json.dumps(feeds, indent=2) + '\n', encoding='utf-8')

    def subscribe(self, ident, tag=None, url=None):
        feeds = json.loads(self.feeds_file.read_text())
        feeds.append({'id': ident, 'url': url or f'{FEED_BASE}/{ident}.xml', 'tag': tag or ident})
        self.write_feeds(feeds)
        return feeds[-1]

    def unsubscribe(self, ident):
        feeds = [f for f in json.loads(self.feeds_file.read_text()) if f['id'] != ident]
        self.write_feeds(feeds)

    def publish(self, ident, items, kind=rss, status=200, headers=None):
        self.http.route(f'{FEED_BASE}/{ident}.xml', kind(items), status, headers)

    # -- runtime ----------------------------------------------------------

    def state(self, dry_run=False):
        state = State(self.data_dir / 'state.db', dry_run=dry_run)
        self.states.append(state)
        self.addCleanup(state.close)
        return state

    def bridge(self, state=None, dry_run=False):
        bridge = Bridge(self.settings, state or self.state(dry_run), dry_run=dry_run, http=self.http)
        bridge.notified = self.sent
        return bridge

    def fake_notify(self, bridge, succeed=True):
        """Capture Telegram messages instead of running a subprocess."""
        import raindrop_rss.bridge as module

        def notify(command, message, timeout=20):
            self.sent.append({'command': tuple(command), 'message': message, 'timeout': timeout})
            return succeed

        original = module.notify
        module.notify = notify
        self.addCleanup(setattr, module, 'notify', original)

    def cutoff(self, state, ident, when):
        state.execute('UPDATE feeds SET added_at=? WHERE id=?', (when, ident))

    def queued(self, state, url):
        rows = state.rows('SELECT * FROM deliveries WHERE url=?', (url,))
        return rows[0] if rows else None

PROJECT = Path(__file__).resolve().parent.parent
