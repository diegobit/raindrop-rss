"""Regressions for the reviewed acceptance findings."""

import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.request

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from raindrop_rss import gui
from raindrop_rss.adapters import never_submitted, retry_time
from raindrop_rss.config import add_feed, generate_id, read_feeds

from .support import COLLECTION, FEED_BASE, Harness, json_response, rss

HOUR = 3600


class GeneratedIdsAvoidHistory(Harness):
    """A retired ID must never be handed to a different publisher."""

    def test_a_removed_tag_does_not_donate_its_cutoff_to_a_new_feed(self):
        old = time.time() - 10 * HOUR
        state = self.state()
        state.register([{'id': 'news'}], old)      # a 'news' feed added long ago
        self.write_feeds([])                       # then removed from the file

        add_feed(self.feeds_file, read_feeds(self.feeds_file)[1],
                 'https://other.test/feed.xml', 'news', state.known_feed_ids())
        saved = json.loads(self.feeds_file.read_text())
        self.assertEqual(saved[0]['id'], 'news-2')

        bridge = self.bridge(state)
        self.assertGreater(state.feed('news-2')['added_at'], old + HOUR,
                           'the new publisher gets a fresh cutoff, not the retired one')
        self.http.route('https://other.test/feed.xml',
                        rss([{'title': 'old back catalogue', 'link': 'https://other.test/1',
                              'published': time.time() - HOUR}]))
        bridge.run_pass()
        self.assertEqual(state.rows('SELECT url FROM deliveries'), [],
                         'articles older than the new cutoff must not be imported')

    def test_the_gui_add_path_passes_history_to_the_generator(self):
        state = self.state()
        state.register([{'id': 'news'}], time.time() - HOUR)
        self.write_feeds([])
        bridge = self.bridge(state)
        bridge.add(bridge.revision, 'https://other.test/feed.xml', 'news')
        self.assertEqual([feed['id'] for feed in bridge.feeds], ['news-2'])

    def test_re_adding_the_same_id_by_hand_still_keeps_its_history(self):
        original = time.time() - 10 * HOUR
        state = self.state()
        state.register([{'id': 'news'}], original)
        self.subscribe('news')
        self.bridge(state)
        self.assertEqual(state.feed('news')['added_at'], original)

    def test_generate_id_accepts_an_explicit_taken_set(self):
        self.assertEqual(generate_id('news', {'news', 'news-2'}), 'news-3')


class RetryAfterIsRespected(Harness):
    def test_a_long_valid_delay_is_not_shortened(self):
        self.assertEqual(retry_time({'Retry-After': '86400'}, 1000) - 1000, 86400)
        self.assertEqual(retry_time({'X-RateLimit-Reset': str(1000 + 90000)}, 1000) - 1000, 90000)

    def test_malformed_and_nonfinite_headers_are_discarded(self):
        for headers in ({'Retry-After': 'nan'}, {'Retry-After': 'inf'}, {'Retry-After': 'soon'},
                        {'X-RateLimit-Reset': 'inf'}, {'X-RateLimit-Reset': '-nan'}, {}):
            with self.subTest(headers=headers):
                self.assertEqual(retry_time(headers, 1000), 1000)

    def test_a_malformed_header_never_clamps_a_valid_one(self):
        headers = {'Retry-After': 'nonsense', 'X-RateLimit-Reset': str(1000 + 7200)}
        self.assertEqual(retry_time(headers, 1000) - 1000, 7200)

    def test_a_day_long_rate_limit_reaches_the_delivery_row(self):
        now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'a', 'link': 'https://a.test/1', 'published': now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.cutoff(state, 'alpha', now - HOUR)
        self.raindrop.create_result = lambda payload: json_response(
            429, {'result': False}, {'Retry-After': '86400'})
        bridge.run_pass()
        row = state.rows('SELECT * FROM deliveries')[0]
        self.assertEqual(row['state'], 'pending')
        self.assertGreater(row['retry_at'], now + 86000)
        self.assertGreater(state.get_meta('remote_retry_at'), now + 86000)


class ConnectionFailureClassification(Harness):
    """A connection that never opened cannot have submitted anything."""

    def refused(self):
        return requests.ConnectionError(MaxRetryError(None, 'u', NewConnectionError(None, 'refused')))

    def dns(self):
        # urllib3 raises NameResolutionError, a NewConnectionError subclass.
        from urllib3.exceptions import NameResolutionError
        return requests.ConnectionError(MaxRetryError(None, 'u', NameResolutionError('h', None, OSError())))

    def reset(self):
        return requests.ConnectionError(ProtocolError('Connection aborted.', ConnectionResetError(104, 'reset')))

    def test_the_classifier_separates_structural_from_ambiguous_failures(self):
        self.assertTrue(never_submitted(self.refused()))
        self.assertTrue(never_submitted(self.dns()))
        self.assertTrue(never_submitted(requests.ConnectTimeout('no route')))
        self.assertFalse(never_submitted(self.reset()))
        self.assertFalse(never_submitted(requests.ReadTimeout('slow')))
        self.assertFalse(never_submitted(ValueError('oversized response')))

    def deliver_with(self, error):
        now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'a', 'link': 'https://a.test/1', 'published': now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.cutoff(state, 'alpha', now - HOUR)
        self.raindrop.create_result = lambda payload: error
        bridge.run_pass()
        return state.rows('SELECT * FROM deliveries')[0]

    def test_a_refused_connection_stays_pending(self):
        row = self.deliver_with(self.refused())
        self.assertEqual(row['state'], 'pending')
        self.assertIn('nothing was submitted', row['error'])

    def test_a_dns_failure_stays_pending(self):
        self.assertEqual(self.deliver_with(self.dns())['state'], 'pending')

    def test_a_reset_mid_request_still_needs_review(self):
        row = self.deliver_with(self.reset())
        self.assertEqual(row['state'], 'needs_review')
        self.assertIn('inspect Raindrop', row['error'])


class PausedUntilTheFirstValidLoad(Harness):
    def test_an_invalid_file_at_startup_holds_back_queued_remote_writes(self):
        state = self.state()
        state.queue([('https://a.test/1', 'queued earlier', 'Alpha', COLLECTION, 'alpha')])
        self.feeds_file.write_text('{ not a list')
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.assertFalse(bridge.loaded)
        bridge.run_pass()
        self.assertEqual(self.raindrop.created, [], 'ingestion is paused, remote writes included')
        self.assertEqual(state.counts()['pending'], 1)

        self.write_feeds([{'id': 'alpha', 'url': f'{FEED_BASE}/alpha.xml', 'tag': 'Alpha'}])
        self.publish('alpha', [])
        bridge.run_pass()
        self.assertTrue(bridge.loaded)
        self.assertEqual(len(self.raindrop.created), 1, 'the first valid load releases the queue')

    def test_a_valid_but_empty_file_is_a_successful_load(self):
        state = self.state()
        state.queue([('https://a.test/1', 'queued earlier', 'Alpha', COLLECTION, 'alpha')])
        bridge = self.bridge(state)          # the harness writes an empty [] file
        self.fake_notify(bridge)
        self.assertTrue(bridge.loaded)
        self.assertEqual(bridge.feeds, [])
        bridge.run_pass()
        self.assertEqual(len(self.raindrop.created), 1,
                         'no subscriptions is not the same as no valid file')

    def test_an_edit_that_breaks_the_file_later_keeps_delivering(self):
        now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'a', 'link': 'https://a.test/1', 'published': now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.cutoff(state, 'alpha', now - HOUR)
        self.raindrop.collection_result = json_response(401, {'result': False})
        bridge.run_pass()
        self.assertEqual(state.counts()['pending'], 1)

        self.feeds_file.write_text('[ broken')
        self.raindrop.collection_result = None
        state.set_meta('remote_retry_at', 0)
        state.execute('UPDATE deliveries SET retry_at=0')
        bridge.run_pass()
        self.assertTrue(bridge.loaded)
        self.assertEqual(len(self.raindrop.created), 1,
                         'a broken later edit keeps the last valid list and the queue moving')


class StaleRemoteErrorClears(Harness):
    def test_resolving_the_last_row_by_hand_clears_the_banner_and_alerts(self):
        now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'a', 'link': 'https://a.test/1', 'published': now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.cutoff(state, 'alpha', now - HOUR)
        self.raindrop.create_result = lambda payload: json_response(502, {'result': False})
        bridge.run_pass()
        self.assertEqual(state.counts()['needs_review'], 1)
        alerts = len(self.sent)

        # No new article will ever arrive; the operator resolves the row by hand.
        state.set_delivery(1, 'saved', bookmark_id=99)
        self.publish('alpha', [])
        bridge.run_pass()
        self.assertIsNone(state.get_meta('remote_error'))
        self.assertEqual(state.get_meta('remote_retry_at'), 0)
        self.assertGreater(len(self.sent), alerts)
        self.assertIn('Resolved:', self.sent[-1]['message'])


class DebugLoggingKeepsSecrets(Harness):
    """--debug must not turn urllib3 or the GUI into a secret leak."""

    def capture(self):
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        return lambda: '\n'.join(record.getMessage() for record in records)

    def test_a_real_request_with_a_secret_query_is_not_logged(self):
        from raindrop_rss.__main__ import configure_logging
        from raindrop_rss.adapters import HTTP

        served = threading.Event()

        class Quiet(gui.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                served.set()
                body = rss([])
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = gui.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        configure_logging(debug=True)
        self.addCleanup(configure_logging, False)
        logs = self.capture()
        url = 'http://127.0.0.1:%d/feed.xml?token=supersecretvalue' % server.server_address[1]
        HTTP().request('GET', url, 5, 100000)
        self.assertTrue(served.is_set())
        self.assertNotIn('supersecretvalue', logs())
        self.assertEqual(logging.getLogger('urllib3').getEffectiveLevel(), logging.WARNING)
        self.assertEqual(logging.getLogger('raindrop_rss').getEffectiveLevel(), logging.DEBUG)

    def test_the_gui_never_logs_a_request_path(self):
        from raindrop_rss.__main__ import configure_logging
        configure_logging(debug=True)
        self.addCleanup(configure_logging, False)
        logs = self.capture()
        state = self.state()
        settings = type(self.settings)(**{**self.settings.__dict__, 'bind': '127.0.0.1', 'port': 0})
        server = gui.Server(self.bridge(state), settings)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        with urllib.request.urlopen(base + '/?debug=supersecretvalue', timeout=5) as response:
            response.read()
        self.assertNotIn('supersecretvalue', logs())


class FeedFilePermissions(Harness):
    def test_a_private_feed_file_stays_private_after_the_gui_saves(self):
        self.subscribe('alpha', url=f'{FEED_BASE}/alpha.xml?key=supersecret')
        os.chmod(self.feeds_file, 0o600)
        feeds, revision = read_feeds(self.feeds_file)
        add_feed(self.feeds_file, revision, 'https://new.test/feed.xml', 'New')
        self.assertEqual(stat.S_IMODE(self.feeds_file.stat().st_mode), 0o600)

    def test_an_ordinary_feed_file_keeps_its_mode_too(self):
        self.subscribe('alpha')
        os.chmod(self.feeds_file, 0o640)
        add_feed(self.feeds_file, read_feeds(self.feeds_file)[1], 'https://new.test/f', 'New')
        self.assertEqual(stat.S_IMODE(self.feeds_file.stat().st_mode), 0o640)


class NonAsciiFormToken(Harness):
    def test_a_non_ascii_token_is_rejected_instead_of_crashing(self):
        page = gui.Page(self.bridge())
        message, bad, url, tag = page.add({'token': ['tökén'], 'url': ['https://a.test/f'], 'tag': ['A']})
        self.assertTrue(bad)
        self.assertIn('Form token rejected', message)
        self.assertEqual(json.loads(self.feeds_file.read_text()), [])

    def test_the_real_token_still_works(self):
        bridge = self.bridge()
        page = gui.Page(bridge)
        message, bad, _, _ = page.add({'token': [page.token], 'revision': [bridge.revision],
                                       'url': ['https://a.test/f'], 'tag': ['A']})
        self.assertFalse(bad, message)


class DatabaseFailuresAreReported(Harness):
    def test_the_add_form_does_not_claim_success_when_registration_fails(self):
        bridge = self.bridge()
        page = gui.Page(bridge)
        bridge.state.db.close()          # simulate an unusable database
        message, bad, _, _ = page.add({'token': [page.token], 'revision': [bridge.revision],
                                       'url': ['https://a.test/f'], 'tag': ['A']})
        self.assertTrue(bad)
        self.assertIn('recording its cutoff failed', message)
        self.assertNotIn('scheduled', message)

    def test_the_status_page_answers_500_when_the_database_is_gone(self):
        state = self.state()
        settings = type(self.settings)(**{**self.settings.__dict__, 'bind': '127.0.0.1', 'port': 0})
        server = gui.Server(self.bridge(state), settings)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        state.db.close()
        request = urllib.request.Request('http://127.0.0.1:%d/' % server.server_address[1])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 500)
        self.assertNotIn('Traceback', caught.exception.read().decode())
