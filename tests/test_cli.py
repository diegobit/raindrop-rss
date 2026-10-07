"""Entry points: dry-run purity, the exclusive lock, review repair, shutdown."""

import contextlib
import hashlib
import io
import json
import socket
import threading
import time

from raindrop_rss import __main__ as cli
from raindrop_rss import bridge as bridge_module
from raindrop_rss import gui
from raindrop_rss.state import writer_lock

from .support import API, Harness, json_response

HOUR = 3600


class Command(Harness):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.config = self.root / 'config' / 'settings.toml'
        self.port = free_port()
        self.config.write_text(
            f'collection_id = {self.settings.collection_id}\n'
            'feeds_file = "../subscriptions/feeds.json"\n'
            'data_dir = "../data"\n'
            'token_file = "raindrop-token"\n'
            f'port = {self.port}\nbind = "127.0.0.1"\n'
            'tg_command = ["tg", "-p", "alerts"]\n')
        # Every entry point shares one fake network and one notification sink.
        self.patch(bridge_module, 'HTTP', lambda: self.http)
        self.patch(bridge_module, 'notify',
                   lambda command, message, timeout=20: self.sent.append(message) or True)
        self.listeners = []
        self.patch(gui, 'serve', lambda bridge, settings: self.listeners.append(settings))

    def capture_logs(self):
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger('raindrop_rss')
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        return lambda: '\n'.join(record.getMessage() for record in records)

    def patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(['--config', str(self.config), *argv])
        return code, out.getvalue()


class DryRun(Command):
    def setUp(self):
        super().setUp()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'fresh', 'link': 'https://a.test/1', 'published': self.now - 60}])

    def digest(self):
        state = self.data_dir / 'state.db'
        return {
            'db': hashlib.sha256(state.read_bytes()).hexdigest() if state.exists() else None,
            'feeds': hashlib.sha256(self.feeds_file.read_bytes()).hexdigest(),
            'files': sorted(p.name for p in self.data_dir.iterdir()),
        }

    def test_a_dry_run_on_a_fresh_install_writes_nothing(self):
        before = self.digest()
        code, _ = self.run_cli('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.digest()['files'], [], 'no database and no writer.lock')

    def test_a_dry_run_leaves_an_existing_database_byte_for_byte_identical(self):
        self.run_cli('--once')
        before = self.digest()
        self.assertIsNotNone(before['db'])
        code, _ = self.run_cli('--dry-run')
        self.assertEqual(code, 0)
        self.assertEqual(self.digest(), before)

    def test_a_dry_run_sends_nothing_to_raindrop_or_telegram(self):
        self.run_cli('--dry-run')
        self.assertEqual(self.raindrop.created, [])
        self.assertEqual(self.sent, [])
        self.assertEqual([call['method'] for call in self.http.calls], ['GET'])

    def test_a_dry_run_opens_no_listener(self):
        self.run_cli('--dry-run')
        self.assertEqual(self.listeners, [])
        with socket.socket() as probe:
            probe.settimeout(1)
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', self.port)), 0)

    def test_a_new_feed_gets_its_cutoff_in_ram_so_nothing_is_previewed_yet(self):
        logs = self.capture_logs()
        self.assertEqual(self.run_cli('--dry-run')[0], 0)
        self.assertNotIn('Preview candidate', logs())
        self.assertFalse((self.data_dir / 'state.db').exists())

    def test_the_preview_lists_candidates_against_the_persisted_cutoff(self):
        state = self.state()
        state.register([{'id': 'alpha'}], self.now - HOUR)
        state.close()
        logs = self.capture_logs()
        self.assertEqual(self.run_cli('--dry-run')[0], 0)
        self.assertIn('Preview candidate: https://a.test/1', logs())
        self.assertEqual(self.raindrop.created, [])

    def test_a_dry_run_runs_while_the_service_holds_the_lock(self):
        with writer_lock(self.data_dir):
            code, _ = self.run_cli('--dry-run')
        self.assertEqual(code, 0)


class SinglePass(Command):
    def test_once_imports_and_exits_without_a_listener(self):
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'fresh', 'link': 'https://a.test/1', 'published': self.now - 60}])
        state = self.state()
        state.register([{'id': 'alpha'}], self.now - HOUR)
        state.close()

        code, _ = self.run_cli('--once')
        self.assertEqual(code, 0)
        self.assertEqual(len(self.raindrop.created), 1)
        self.assertEqual(self.listeners, [])
        self.assertTrue((self.data_dir / 'writer.lock').exists())

    def test_a_second_writer_is_refused(self):
        logs = self.capture_logs()
        with writer_lock(self.data_dir):
            code, _ = self.run_cli('--once')
        self.assertEqual(code, 3)
        self.assertIn('Another bridge process', logs())

    def test_an_unreadable_settings_file_exits_with_a_message(self):
        self.config.write_text('not [ toml')
        code, _ = self.run_cli('--once')
        self.assertEqual(code, 2)


class Review(Command):
    def setUp(self):
        super().setUp()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'ambiguous', 'link': 'https://a.test/1',
                                'published': self.now - 60}])
        state = self.state()
        state.register([{'id': 'alpha'}], self.now - HOUR)
        state.close()
        self.raindrop.create_result = lambda payload: json_response(502, {'result': False})
        self.run_cli('--once')
        self.raindrop.create_result = None

    def rows(self):
        state = self.state()
        rows = state.rows('SELECT * FROM deliveries')
        state.close()
        return rows

    def test_list_shows_the_exact_url_and_the_reason(self):
        code, output = self.run_cli('review', 'list')
        self.assertEqual(code, 0)
        self.assertIn('https://a.test/1', output)
        self.assertIn('Ambiguous', output)
        self.assertIn('row 1', output)

    def test_marking_a_row_saved_records_the_bookmark_and_stops_resending(self):
        code, output = self.run_cli('review', 'saved', '1', '987')
        self.assertEqual(code, 0)
        row = self.rows()[0]
        self.assertEqual((row['state'], row['bookmark_id']), ('saved', 987))
        self.run_cli('--once')
        self.assertEqual(self.raindrop.created, [])

    def test_requeueing_a_row_sends_it_on_the_next_pass(self):
        code, _ = self.run_cli('review', 'requeue', '1')
        self.assertEqual(code, 0)
        self.assertEqual(self.rows()[0]['state'], 'pending')
        self.run_cli('--once')
        self.assertEqual(len(self.raindrop.created), 1)
        self.assertEqual(self.rows()[0]['state'], 'saved')

    def test_an_unknown_row_is_an_error(self):
        code, output = self.run_cli('review', 'saved', '99', '1')
        self.assertEqual(code, 1)
        self.assertIn('No delivery 99', output)

    def test_review_refuses_to_run_beside_the_service(self):
        with writer_lock(self.data_dir):
            code, _ = self.run_cli('review', 'list')
        self.assertEqual(code, 3)

    def test_nothing_to_review_says_so(self):
        self.run_cli('review', 'saved', '1', '987')
        code, output = self.run_cli('review', 'list')
        self.assertIn('Nothing needs review', output)


class Shutdown(Harness):
    def test_the_loop_stops_promptly_on_a_signal(self):
        self.subscribe('alpha')
        self.publish('alpha', [])
        bridge = self.bridge()
        self.fake_notify(bridge)
        finished = threading.Event()
        threading.Thread(target=lambda: (bridge.run(), finished.set()), daemon=True).start()
        time.sleep(0.2)
        bridge.shutdown()
        self.assertTrue(finished.wait(5), 'run() must return once stop is set')

    def test_a_stop_during_delivery_leaves_the_queue_pending(self):
        now = time.time()
        self.publish('alpha', [{'title': f'a{n}', 'link': f'https://a.test/{n}',
                                'published': now - 60} for n in range(4)])
        self.subscribe('alpha')
        state = self.state()
        bridge = self.bridge(state)
        self.fake_notify(bridge)
        self.cutoff(state, 'alpha', now - HOUR)
        create = self.raindrop.on_create

        def stop_after_the_first(method, url, kwargs):
            bridge.stop.set()
            return create(method, url, kwargs)

        self.http.routes[API + '/raindrop'] = stop_after_the_first
        bridge.run_pass()
        counts = state.counts()
        self.assertEqual(counts['saved'], 1)
        self.assertEqual(counts['pending'], 3, 'unfinished work stays queued for the next pass')
        self.assertEqual(counts['sending'], 0)


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]
