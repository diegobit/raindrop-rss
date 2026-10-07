"""Telegram alerts: argument handling, suppression, retry, and redaction."""

import json
import stat
import time

import requests

from raindrop_rss.adapters import notify

from .support import FEED_BASE, Harness, json_response, rss

HOUR = 3600


class NotifyCommand(Harness):
    """The real subprocess call, against a recording stub instead of `tg`."""

    def stub(self, body='exit 0'):
        script = self.root / 'tg-stub'
        record = self.root / 'tg-argv.json'
        script.write_text('#!/bin/sh\n'
                          'python3 -c \'import json,sys; open(sys.argv[1],"w").write(json.dumps(sys.argv[2:]))\' '
                          f'"{record}" "$@"\n{body}\n')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script, record

    def test_the_message_is_one_argument_after_the_command_array(self):
        script, record = self.stub()
        message = 'Raindrop RSS\nFailures:\n$(rm -rf /) `id` ; echo pwned'
        self.assertTrue(notify((str(script), '-p', 'alerts'), message))
        self.assertEqual(json.loads(record.read_text()), ['-p', 'alerts', message])

    def test_a_leading_dash_stays_inside_the_message_argument(self):
        script, record = self.stub()
        self.assertTrue(notify((str(script), '--'), '-not-an-option'))
        self.assertEqual(json.loads(record.read_text()), ['--', '-not-an-option'])

    def test_a_failing_command_reports_failure_instead_of_raising(self):
        script, _ = self.stub('exit 1')
        self.assertFalse(notify((str(script),), 'hello'))

    def test_a_missing_command_reports_failure(self):
        self.assertFalse(notify((str(self.root / 'absent'),), 'hello'))

    def test_a_hanging_command_is_killed_by_the_timeout(self):
        script, _ = self.stub('sleep 30')
        started = time.monotonic()
        self.assertFalse(notify((str(script),), 'hello', timeout=1))
        self.assertLess(time.monotonic() - started, 10)


class Incidents(Harness):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.settings = type(self.settings)(**{**self.settings.__dict__,
                                               'tg_command': ('tg', '-p', 'alerts')})
        self.url = f'{FEED_BASE}/alpha.xml?key=supersecret'
        self.subscribe('alpha', tag='Alpha', url=self.url)
        self.db = self.state()

    def bridge_with_notify(self, succeed=True):
        bridge = self.bridge(self.db)
        self.fake_notify(bridge, succeed)
        return bridge

    def messages(self):
        return [entry['message'] for entry in self.sent]

    def test_a_healthy_pass_sends_nothing(self):
        self.http.route(self.url, rss([]))
        self.bridge_with_notify().run_pass()
        self.assertEqual(self.db.feed('alpha')['health'], 'healthy')
        self.assertEqual(self.sent, [])

    def test_one_alert_per_incident_then_silence_then_one_recovery(self):
        self.http.fail(self.url, requests.ConnectionError('down'))
        bridge = self.bridge_with_notify()
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('Failures:', self.messages()[0])
        self.assertIn('Publisher failure', self.messages()[0])

        bridge.run_pass()
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1, 'an unchanged incident must stay quiet')

        self.http.route(self.url, rss([]))
        bridge.run_pass()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Resolved:', self.messages()[1])

    def test_the_message_never_contains_secret_url_query_values(self):
        self.http.fail(self.url, requests.ConnectionError('down'))
        self.bridge_with_notify().run_pass()
        self.assertNotIn('supersecret', self.messages()[0])
        self.assertIn('redacted', self.messages()[0])

    def test_the_configured_profile_is_used_verbatim(self):
        self.http.fail(self.url, requests.ConnectionError('down'))
        self.bridge_with_notify().run_pass()
        self.assertEqual(self.sent[0]['command'], ('tg', '-p', 'alerts'))

    def test_a_failed_notification_is_retried_on_the_next_pass(self):
        self.http.fail(self.url, requests.ConnectionError('down'))
        bridge = self.bridge_with_notify(succeed=False)
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.db.get_meta('notified_incidents', {}), {})
        bridge.run_pass()
        self.assertEqual(len(self.sent), 2, 'an unacknowledged alert must be retried')

        self.fake_notify(bridge, succeed=True)
        bridge.run_pass()
        self.assertEqual(len(self.sent), 3)
        bridge.run_pass()
        self.assertEqual(len(self.sent), 3, 'once delivered it goes quiet again')

    def test_incident_suppression_survives_a_restart(self):
        self.http.fail(self.url, requests.ConnectionError('down'))
        self.bridge_with_notify().run_pass()
        self.assertEqual(len(self.sent), 1)
        self.db.close()
        restarted = self.state()
        bridge = self.bridge(restarted)
        self.fake_notify(bridge)
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1, 'a restart must not repeat the alert')

    def test_missing_dates_are_reported_as_one_aggregate_count(self):
        items = [{'title': f'n{n}', 'link': f'https://a.test/{n}'} for n in range(12)]
        self.http.route(self.url, rss(items))
        bridge = self.bridge_with_notify()
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1)
        self.assertIn('12 entries without a publication date', self.messages()[0])
        bridge.run_pass()
        self.assertEqual(len(self.sent), 1)

    def test_a_review_row_and_a_raindrop_halt_are_both_reported(self):
        self.http.route(self.url,
                        rss([{'title': 'a', 'link': 'https://a.test/1', 'published': self.now - 60}]))
        self.raindrop.collection_result = json_response(401, {'result': False})
        bridge = self.bridge_with_notify()
        self.cutoff(self.db, 'alpha', self.now - HOUR)
        bridge.run_pass()
        self.assertIn('credentials', self.messages()[0])

    def test_a_configuration_error_is_an_incident(self):
        self.feeds_file.write_text('nonsense')
        bridge = self.bridge_with_notify()
        bridge.run_pass()
        self.assertIn('Feed configuration invalid', self.messages()[0])

    def test_a_long_incident_list_is_truncated_for_telegram(self):
        for index in range(200):
            self.subscribe(f'broken{index:03d}')
        bridge = self.bridge_with_notify()
        bridge.run_pass()
        self.assertLessEqual(len(self.messages()[0]), 3500)


class ReviewAlerts(Harness):
    """Each ambiguous delivery is announced once, and once again when resolved."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.db = self.state()
        self.publish('alpha', [{'title': f'a{n}', 'link': f'https://a.test/{n}',
                                'published': self.now - 60} for n in range(2)])
        self.raindrop.create_result = lambda payload: json_response(502, {'result': False})

    def test_two_review_rows_produce_two_alerts_and_one_recovery(self):
        bridge = self.bridge(self.db)
        self.fake_notify(bridge)
        self.cutoff(self.db, 'alpha', self.now - HOUR)
        bridge.run_pass()
        first = [entry['message'] for entry in self.sent]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].count('needs manual review'), 2)

        bridge.run_pass()
        self.assertEqual(len(self.sent), 1, 'unchanged review rows stay quiet')

        self.db.set_delivery(1, 'saved', bookmark_id=5)
        bridge.run_pass()
        self.assertEqual(len(self.sent), 2)
        self.assertIn('Resolved:', self.sent[1]['message'])
        self.assertEqual(self.sent[1]['message'].count('needs manual review'), 1)
