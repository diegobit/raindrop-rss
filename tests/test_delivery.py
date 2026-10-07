"""Delivery states, durability, and the manual review path."""

import time

import requests

from raindrop_rss.adapters import DeadlineExpired
from raindrop_rss.state import writer_lock

from .support import API, COLLECTION, Harness, json_response

HOUR = 3600


class Delivery(Harness):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.subscribe('alpha', tag='Alpha')
        self.publish('alpha', [{'title': 'One <b>bold</b>', 'link': 'https://a.test/1',
                                'published': self.now - 60}])
        self.db = self.state()

    def pass_once(self, state=None):
        bridge = self.bridge(state or self.db)
        self.cutoff(bridge.state, 'alpha', self.now - HOUR)
        bridge.run_pass()
        return bridge

    def only(self, state=None):
        return (state or self.db).rows('SELECT * FROM deliveries ORDER BY id')[0]

    # -- the happy path ---------------------------------------------------

    def test_a_created_bookmark_records_its_id_and_strips_markup(self):
        self.pass_once()
        row = self.only()
        self.assertEqual(row['state'], 'saved')
        self.assertEqual(row['bookmark_id'], 1001)
        self.assertEqual(row['title'], 'One bold')
        payload = self.raindrop.created[0]
        self.assertEqual(payload, {'link': 'https://a.test/1', 'collection': {'$id': COLLECTION},
                                   'tags': ['Alpha'], 'title': 'One bold'})

    def test_the_token_is_sent_as_a_bearer_header_and_never_stored(self):
        self.pass_once()
        post = [call for call in self.http.calls if call['method'] == 'POST'][0]
        self.assertEqual(post['headers']['Authorization'], 'Bearer test-token-not-a-real-secret')
        for row in self.db.rows('SELECT * FROM deliveries') + self.db.rows('SELECT * FROM meta'):
            self.assertNotIn('test-token-not-a-real-secret', repr(row))

    # -- temporary rejections stay pending --------------------------------

    def test_bad_credentials_halt_the_pass_and_keep_work_pending(self):
        self.raindrop.collection_result = json_response(401, {'result': False})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertIn('credentials', self.db.get_meta('remote_error'))
        self.assertEqual(self.raindrop.created, [])

    def test_an_invalid_destination_halts_rather_than_falling_back_to_unsorted(self):
        self.raindrop.collection_result = json_response(404, {'result': False})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertIn('Destination', self.db.get_meta('remote_error'))
        self.assertEqual(self.raindrop.created, [])

    def test_a_destination_the_account_cannot_write_to_is_refused(self):
        self.raindrop.collection_result = json_response(
            200, {'result': True, 'item': {'_id': COLLECTION, 'access': {'level': 1}}})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertIn('not writable', self.db.get_meta('remote_error'))

    def test_a_rate_limit_stays_pending_and_respects_the_reset_header(self):
        reset = self.now + 900
        self.raindrop.create_result = lambda payload: json_response(
            429, {'result': False}, {'X-RateLimit-Reset': str(int(reset))})
        self.pass_once()
        row = self.only()
        self.assertEqual(row['state'], 'pending')
        self.assertGreaterEqual(row['retry_at'], reset - 1)
        self.assertGreaterEqual(self.db.get_meta('remote_retry_at'), reset - 1)

    def test_retry_after_seconds_is_honoured_when_longer_than_the_interval(self):
        self.raindrop.create_result = lambda payload: json_response(
            429, {'result': False}, {'Retry-After': '7200'})
        self.pass_once()
        self.assertGreater(self.only()['retry_at'], self.now + 7000)

    def test_a_connect_timeout_before_submission_stays_pending(self):
        self.raindrop.create_result = lambda payload: requests.ConnectTimeout('no connection')
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertIn('nothing was submitted', self.only()['error'])

    def test_a_pending_row_is_retried_on_a_later_pass_and_then_saved(self):
        self.raindrop.collection_result = json_response(401, {'result': False})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.raindrop.collection_result = None
        self.db.set_meta('remote_retry_at', 0)
        self.db.execute('UPDATE deliveries SET retry_at=0')
        self.pass_once()
        self.assertEqual(self.only()['state'], 'saved')
        self.assertIsNone(self.db.get_meta('remote_error'))

    # -- ambiguity goes to review, never to a blind resend ----------------

    def test_a_timeout_after_possible_submission_needs_review(self):
        self.raindrop.create_result = lambda payload: DeadlineExpired('read timed out')
        self.pass_once()
        self.assertEqual(self.only()['state'], 'needs_review')
        self.assertIn('inspect Raindrop', self.only()['error'])

    def test_a_review_row_is_never_resent_by_a_later_pass(self):
        self.raindrop.create_result = lambda payload: DeadlineExpired('read timed out')
        self.pass_once()
        self.raindrop.create_result = None
        self.pass_once()
        self.pass_once()
        self.assertEqual(self.raindrop.created, [])
        self.assertEqual(self.only()['state'], 'needs_review')

    def test_an_ambiguous_server_response_needs_review(self):
        for response in (json_response(200, {'result': True, 'item': {}}),
                         json_response(200, {'result': False, 'errorMessage': 'huh'}),
                         json_response(502, {'result': False}),
                         json_response(200, {'result': True, 'item': {'_id': 'not-an-int'}})):
            with self.subTest(status=response.status):
                self.setUp()
                self.raindrop.create_result = lambda payload, r=response: r
                self.pass_once()
                self.assertEqual(self.only()['state'], 'needs_review')

    def test_a_bookmark_created_outside_the_destination_needs_review(self):
        self.raindrop.create_result = lambda payload: json_response(
            200, {'result': True, 'item': {'_id': 55, 'collection': {'$id': 0}}})
        self.pass_once()
        row = self.only()
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['bookmark_id'], 55)

    def test_a_rejected_payload_needs_review(self):
        self.raindrop.create_result = lambda payload: json_response(400, {'result': False})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'needs_review')
        self.assertIn('payload', self.only()['error'])

    def test_a_crash_while_sending_becomes_review_on_restart(self):
        self.pass_once()
        self.db.execute("UPDATE deliveries SET state='sending', bookmark_id=NULL")
        self.db.close()
        restarted = self.state()
        row = self.only(restarted)
        self.assertEqual(row['state'], 'needs_review')
        self.assertIn('Interrupted', row['error'])
        self.pass_once(restarted)
        self.assertEqual(len(self.raindrop.created), 1, 'the interrupted row must not be resent')

    # -- durability -------------------------------------------------------

    def test_pending_work_survives_a_restart_and_feed_removal(self):
        self.raindrop.collection_result = json_response(401, {'result': False})
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.db.close()

        self.unsubscribe('alpha')          # feed rollover must not erase the queue
        self.raindrop.collection_result = None
        restarted = self.state()
        restarted.set_meta('remote_retry_at', 0)
        restarted.execute('UPDATE deliveries SET retry_at=0')
        self.bridge(restarted).run_pass()
        self.assertEqual(self.only(restarted)['state'], 'saved')
        self.assertEqual(len(self.raindrop.created), 1)

    def test_delivery_history_outlives_the_feed_so_nothing_is_reimported(self):
        self.pass_once()
        self.unsubscribe('alpha')
        self.bridge(self.db).run_pass()
        self.subscribe('alpha', tag='Alpha')
        self.pass_once()
        self.assertEqual(len(self.raindrop.created), 1)

    def test_a_second_writer_is_rejected_while_the_first_holds_the_lock(self):
        with writer_lock(self.data_dir):
            with self.assertRaises(RuntimeError):
                with writer_lock(self.data_dir):
                    pass
        with writer_lock(self.data_dir):
            pass                            # released cleanly, so a restart works

    def test_a_missing_token_file_leaves_the_queue_intact(self):
        self.token_file.unlink()
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertIn('token', self.db.get_meta('remote_error'))

    def test_a_token_with_embedded_whitespace_is_refused(self):
        self.token_file.write_text('two words\n')
        self.pass_once()
        self.assertEqual(self.only()['state'], 'pending')
        self.assertEqual(self.raindrop.created, [])

    def test_the_destination_is_verified_once_per_pass(self):
        self.publish('alpha', [{'title': f'a{n}', 'link': f'https://a.test/{n}',
                                'published': self.now - 60} for n in range(5)])
        self.pass_once()
        checks = [call for call in self.http.calls
                  if call['url'] == f'{API}/collection/{COLLECTION}']
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(self.raindrop.created), 5)
