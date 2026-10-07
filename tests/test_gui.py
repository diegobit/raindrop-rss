"""The single status page: rendering, the add form, and responsiveness."""

import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import requests

from raindrop_rss import gui
from raindrop_rss.adapters import Response

from .support import FEED_BASE, Harness, rss

HOUR = 3600


class Live(Harness):
    """Runs the real server in a background thread, as production does."""

    def serve(self, bridge):
        self.settings = type(self.settings)(**{**self.settings.__dict__, 'bind': '127.0.0.1', 'port': 0})
        server = gui.Server(bridge, self.settings)
        self.base = 'http://127.0.0.1:%d' % server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def get(self, path='/', timeout=5):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as response:
            return response.status, response.read().decode('utf-8')

    def post(self, fields, path='/add', timeout=5):
        body = '&'.join(f'{key}={urllib.parse.quote(value)}' for key, value in fields.items()).encode()
        request = urllib.request.Request(self.base + path, data=body,
                                         headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode('utf-8')

    def form_fields(self, page=None):
        page = self.get('/add')[1] if page is None else page
        return dict(re.findall(r'name="(token|revision)" value="([^"]*)"', page))


class Rendering(Live):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.subscribe('alpha', tag='Alpha', url=f'{FEED_BASE}/alpha.xml?key=supersecret')
        self.subscribe('beta', tag='Beta')
        self.db = self.state()

    def test_shows_persisted_health_timestamps_and_counts(self):
        self.http.route(f'{FEED_BASE}/alpha.xml?key=supersecret',
                        rss([{'title': 'a', 'link': 'https://a.test/1', 'published': self.now - 60}]))
        self.http.fail(f'{FEED_BASE}/beta.xml', requests.ConnectionError('down'))
        bridge = self.bridge(self.db)
        self.fake_notify(bridge)
        self.cutoff(self.db, 'alpha', self.now - HOUR)
        bridge.run_pass()

        self.serve(bridge)
        status, page = self.get()
        self.assertEqual(status, 200)
        self.assertIn('healthy', page)
        self.assertIn('error', page)
        self.assertIn('Saved: <b>1</b>', page)
        self.assertIn('Needs review: <b>0</b>', page)
        self.assertIn('Pending delivery: <b>0</b>', page)
        self.assertRegex(page, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC')

    def test_never_prints_feed_url_query_values(self):
        bridge = self.bridge(self.db)
        self.serve(bridge)
        _, page = self.get()
        self.assertNotIn('supersecret', page)
        self.assertIn('redacted', page)

    def test_a_never_checked_feed_reads_pending_and_never(self):
        self.serve(self.bridge(self.db))
        _, page = self.get()
        self.assertIn('pending', page)
        self.assertIn('never', page)
        self.assertIn('none', page)

    def test_the_add_button_comes_after_the_list_and_opens_the_form_page(self):
        self.serve(self.bridge(self.db))
        _, page = self.get()
        self.assertLess(page.index('</table>'), page.index('>Add feed</a>'))
        self.assertIn('href="/add"', page)
        self.assertNotIn('<form', page, 'the refreshing page must carry no input fields')

    def test_the_status_page_refreshes_but_the_form_page_does_not(self):
        self.serve(self.bridge(self.db))
        self.assertIn('http-equiv="refresh"', self.get()[1])
        status, form = self.get('/add')
        self.assertEqual(status, 200)
        self.assertNotIn('http-equiv="refresh"', form)
        self.assertIn('name="url"', form)

    def test_html_in_a_tag_or_error_is_escaped(self):
        self.write_feeds([{'id': 'x', 'url': 'https://x.test/f', 'tag': '<script>alert(1)</script>'}])
        bridge = self.bridge(self.db)
        self.db.execute("UPDATE feeds SET health='error', error='<img src=x onerror=alert(1)>' WHERE id='x'")
        self.serve(bridge)
        _, page = self.get()
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<img src=x', page)
        self.assertIn('&lt;script&gt;', page)

    def test_a_configuration_error_is_shown_with_the_last_valid_list(self):
        bridge = self.bridge(self.db)
        self.feeds_file.write_text('[ broken')
        bridge.reload()
        self.serve(bridge)
        _, page = self.get()
        self.assertIn('Feed configuration error', page)
        self.assertIn('Alpha', page)

    def test_each_feed_has_an_icon_and_healthy_stays_on_one_line(self):
        bridge = self.bridge(self.db)
        self.db.execute("UPDATE feeds SET health='healthy' WHERE id='alpha'")
        self.serve(bridge)
        _, page = self.get()
        self.assertIn('white-space: nowrap', page)
        self.assertIn('--ok: light-dark(#0f7a38, #8ed7a6)', page)
        cell = re.search(r'class="health healthy">.*?</td>', page).group(0)
        self.assertIn(' healthy</span>', cell)
        self.assertNotIn('<br', cell)
        self.assertEqual(page.count('class="glyph"'), 2)
        self.assertIn("background-image:url('https://feeds.test/favicon.ico')", page)
        self.assertNotIn('supersecret', page)

    def test_unknown_paths_are_not_found(self):
        self.serve(self.bridge(self.db))
        request = urllib.request.Request(self.base + '/admin')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)


class AddForm(Live):
    def setUp(self):
        super().setUp()
        self.db = self.state()
        self.bridge_ = self.bridge(self.db)
        self.serve(self.bridge_)

    def add(self, url='https://new.test/feed.xml', tag='New', **override):
        fields = {**self.form_fields(), 'url': url, 'tag': tag, **override}
        return self.post(fields)

    def test_an_addition_persists_registers_a_cutoff_and_survives_restart(self):
        before = time.time()
        status, page = self.add()
        self.assertEqual(status, 200)
        self.assertIn('first check is scheduled', page)

        saved = json.loads(self.feeds_file.read_text())
        self.assertEqual(saved, [{'id': 'new', 'url': 'https://new.test/feed.xml', 'tag': 'New'}])
        self.assertGreaterEqual(self.db.feed('new')['added_at'], before)
        self.assertTrue(self.bridge_.wake.is_set(), 'the new feed is scheduled immediately')

        self.db.close()
        restarted = self.state()
        self.assertEqual([f['id'] for f in self.bridge(restarted).feeds], ['new'])

    def test_a_wrong_form_token_is_rejected(self):
        status, page = self.add(token='forged')
        self.assertEqual(status, 200)
        self.assertIn('Form token rejected', page)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])

    def test_a_missing_form_token_is_rejected(self):
        status, body = self.post({'revision': self.form_fields()['revision'],
                                  'url': 'https://new.test/f', 'tag': 'New'})
        self.assertIn('Form token rejected', body)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])

    def test_a_conflicting_manual_edit_is_refused_without_data_loss(self):
        fields = self.form_fields()
        self.subscribe('manual')
        before = self.feeds_file.read_text()
        status, body = self.post({**fields, 'url': 'https://new.test/f', 'tag': 'New'})
        self.assertIn('Feed file changed', body)
        self.assertEqual(self.feeds_file.read_text(), before)

    def test_a_duplicate_url_is_refused(self):
        self.add(url='https://dup.test/feed.xml', tag='One')
        status, body = self.add(url='https://DUP.test/feed.xml', tag='Two')
        self.assertIn('Duplicate', body)
        self.assertEqual(len(json.loads(self.feeds_file.read_text())), 1)

    def test_an_invalid_url_is_refused(self):
        for bad in ('not a url', 'ftp://x.test/f', 'javascript:alert(1)'):
            status, body = self.add(url=bad)
            self.assertIn('URL must be', body)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])

    def test_a_blank_tag_is_refused(self):
        status, body = self.add(tag='   ')
        self.assertIn('source tag is required', body)

    def test_an_unreachable_publisher_can_still_be_added(self):
        self.add(url='https://offline.test/feed.xml', tag='Offline')
        self.http.fail('https://offline.test/feed.xml', requests.ConnectionError('down'))
        self.fake_notify(self.bridge_)
        self.bridge_.run_pass()
        self.assertEqual(self.db.feed('offline')['health'], 'error')
        _, page = self.get()
        self.assertIn('Offline', page)

    def test_a_write_failure_is_reported_without_claiming_success(self):
        directory = self.feeds_file.parent
        mode = directory.stat().st_mode
        os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
        try:
            status, body = self.add()
        finally:
            os.chmod(directory, mode)
        self.assertIn('Could not save', body)
        self.assertNotIn('first check is scheduled', body)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])

    def test_an_oversized_form_body_is_refused(self):
        status, _ = self.post({**self.form_fields(), 'url': 'https://x.test/' + 'a' * 6000, 'tag': 'x'})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])


class RemoveFeed(Live):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.db = self.state()
        self.bridge_ = self.bridge(self.db)
        self.cutoff(self.db, 'alpha', self.now - HOUR)
        self.serve(self.bridge_)

    def confirm(self):
        status, page = self.get('/remove?id=alpha')
        self.assertEqual(status, 200)
        self.assertNotIn('http-equiv="refresh"', page)
        self.assertIn('Remove this feed?', page)
        self.assertIn('Alpha', page)
        return dict(re.findall(r'name="(token|revision|id)" value="([^"]*)"', page))

    def test_remove_asks_for_confirmation_then_stops_polling(self):
        _, page = self.get()
        self.assertIn('href="/remove?id=alpha"', page)
        self.assertNotIn('<form', page)
        fields = self.confirm()
        status, done = self.post(fields, path='/remove')
        self.assertEqual(status, 200)
        self.assertIn('Removed.', done)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])
        self.assertEqual(self.db.feed('alpha')['added_at'], self.now - HOUR)
        self.assertNotIn('Alpha', self.get()[1])

    def test_a_wrong_token_does_not_remove_the_feed(self):
        fields = self.confirm()
        _, page = self.post({**fields, 'token': 'forged'}, path='/remove')
        self.assertIn('Form token rejected', page)
        self.assertEqual([item['id'] for item in json.loads(self.feeds_file.read_text())], ['alpha'])

    def test_a_conflicting_edit_is_refused(self):
        fields = self.confirm()
        self.subscribe('manual')
        before = self.feeds_file.read_text()
        _, page = self.post(fields, path='/remove')
        self.assertIn('Feed file changed', page)
        self.assertEqual(self.feeds_file.read_text(), before)


class Responsiveness(Live):
    def test_the_page_answers_while_a_publisher_stalls(self):
        """Ingestion holds the main thread; the GUI thread must not wait for it."""
        release = threading.Event()
        self.subscribe('slow')

        def stall(method, url, kwargs):
            release.wait(10)
            return Response(200, {}, rss([]), url)

        self.http.routes[f'{FEED_BASE}/slow.xml'] = stall
        db = self.state()
        bridge = self.bridge(db)
        self.fake_notify(bridge)
        self.serve(bridge)

        finished = threading.Event()
        threading.Thread(target=lambda: (bridge.run_pass(), finished.set()), daemon=True).start()
        time.sleep(0.2)

        started = time.monotonic()
        status, page = self.get(timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertIn('Pass running.', page)
        self.assertLess(elapsed, 2, 'the page must not block on the stalled fetch')

        release.set()
        self.assertTrue(finished.wait(10))
        self.assertEqual(db.feed('slow')['health'], 'healthy')
