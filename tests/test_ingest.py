"""Fetch, publication cutoff, deduplication, and feed health."""

import time

import requests

from raindrop_rss.adapters import DeadlineExpired

from .support import COLLECTION, FEED_BASE, Harness, atom, rss

HOUR = 3600


class Cutoff(Harness):
    """Only articles published strictly after the feed was added are imported."""

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.added = self.now - 10 * HOUR
        self.subscribe('alpha', tag='Alpha')

    def run_with(self, items, kind=rss, state=None):
        self.publish('alpha', items, kind=kind)
        bridge = self.bridge(state)
        self.cutoff(bridge.state, 'alpha', self.added)
        bridge.run_pass()
        return bridge

    def test_imports_only_articles_newer_than_the_cutoff(self):
        bridge = self.run_with([
            {'title': 'older', 'link': 'https://a.test/old', 'published': self.added - HOUR},
            {'title': 'equal', 'link': 'https://a.test/equal', 'published': self.added},
            {'title': 'newer', 'link': 'https://a.test/new', 'published': self.added + HOUR},
        ])
        urls = [row['url'] for row in bridge.state.rows('SELECT url FROM deliveries')]
        self.assertEqual(urls, ['https://a.test/new'])

    def test_an_old_article_edited_later_is_not_imported(self):
        """Atom `updated` must never establish eligibility."""
        bridge = self.run_with([
            {'title': 'edited old', 'link': 'https://a.test/edited',
             'published': self.added - HOUR, 'updated': self.now - 60},
        ], kind=atom)
        self.assertEqual(bridge.state.rows('SELECT url FROM deliveries'), [])

    def test_an_atom_entry_with_only_updated_is_treated_as_undated(self):
        bridge = self.run_with([
            {'title': 'no published field', 'link': 'https://a.test/u', 'updated': self.now - 60},
        ], kind=atom)
        self.assertEqual(bridge.state.rows('SELECT url FROM deliveries'), [])
        self.assertEqual(bridge.state.feed('alpha')['missing_dates'], 1)

    def test_undated_and_unparseable_entries_are_skipped_and_counted(self):
        bridge = self.run_with([
            {'title': 'no date', 'link': 'https://a.test/nodate'},
            {'title': 'bad date', 'link': 'https://a.test/baddate', 'raw_date': 'sometime last Tuesday'},
            {'title': 'good', 'link': 'https://a.test/good', 'published': self.now - 60},
        ])
        self.assertEqual(bridge.state.feed('alpha')['missing_dates'], 2)
        self.assertEqual([r['url'] for r in bridge.state.rows('SELECT url FROM deliveries')],
                         ['https://a.test/good'])

    def test_a_future_article_waits_until_its_timestamp_is_current(self):
        state = self.state()
        self.run_with([{'title': 'future', 'link': 'https://a.test/future',
                        'published': self.now + 2 * HOUR}], state=state)
        self.assertEqual(state.rows('SELECT url FROM deliveries'), [])
        # The same entry, once its timestamp is in the past.
        self.publish('alpha', [{'title': 'future', 'link': 'https://a.test/future',
                                'published': self.now - 60}])
        self.bridge(state).run_pass()
        self.assertEqual([r['url'] for r in state.rows('SELECT url FROM deliveries')],
                         ['https://a.test/future'])

    def test_relative_article_links_resolve_against_the_feed_url(self):
        bridge = self.run_with([{'title': 'rel', 'link': '/posts/one.html', 'published': self.now - 60}])
        self.assertEqual([r['url'] for r in bridge.state.rows('SELECT url FROM deliveries')],
                         [f'{FEED_BASE}/posts/one.html'])

    def test_malformed_entries_do_not_discard_the_healthy_ones(self):
        bridge = self.run_with([
            {'title': 'no link', 'published': self.now - 60},
            {'title': 'bad scheme', 'link': 'javascript:alert(1)', 'published': self.now - 60},
            {'title': 'fine', 'link': 'https://a.test/fine', 'published': self.now - 60},
        ])
        self.assertEqual([r['url'] for r in bridge.state.rows('SELECT url FROM deliveries')],
                         ['https://a.test/fine'])
        self.assertEqual(bridge.state.feed('alpha')['health'], 'healthy')

    def test_the_cutoff_survives_a_failed_first_fetch_and_a_restart(self):
        self.http.fail(f'{FEED_BASE}/alpha.xml', requests.ConnectionError('down'))
        state = self.state()
        self.bridge(state).run_pass()
        registered = state.feed('alpha')['added_at']
        self.assertEqual(state.feed('alpha')['health'], 'error')
        state.close()

        later = self.state()
        self.publish('alpha', [{'title': 'published while down', 'link': 'https://a.test/x',
                                'published': registered - 60}])
        self.bridge(later).run_pass()
        self.assertEqual(later.feed('alpha')['added_at'], registered)
        self.assertEqual(later.rows('SELECT url FROM deliveries'), [],
                         'an article older than the cutoff must stay out')

    def test_removing_and_re_adding_a_feed_keeps_its_original_cutoff(self):
        state = self.state()
        self.publish('alpha', [])
        self.bridge(state).run_pass()
        original = state.feed('alpha')['added_at']
        self.unsubscribe('alpha')
        self.bridge(state).run_pass()
        self.subscribe('alpha', tag='Alpha')
        self.bridge(state).run_pass()
        self.assertEqual(state.feed('alpha')['added_at'], original)


class Deduplication(Harness):
    def test_twenty_feeds_deliver_once_and_a_rerun_creates_nothing(self):
        now = time.time()
        for index in range(20):
            ident = f'feed{index:02d}'
            self.subscribe(ident, tag=f'tag{index:02d}')
            kind = atom if index % 2 else rss
            self.publish(ident, [
                {'title': f'{ident} one', 'link': f'https://pub.test/{ident}/1', 'published': now - 60},
                {'title': f'{ident} two', 'link': f'https://pub.test/{ident}/2', 'published': now - 30},
            ], kind=kind)
        state = self.state()
        bridge = self.bridge(state)
        for feed in bridge.feeds:
            self.cutoff(state, feed['id'], now - HOUR)
        bridge.run_pass()

        self.assertEqual(len(self.raindrop.created), 40)
        self.assertEqual(state.counts()['saved'], 40)
        tags = {payload['tags'][0] for payload in self.raindrop.created}
        self.assertEqual(tags, {f'tag{index:02d}' for index in range(20)})
        self.assertTrue(all(p['collection'] == {'$id': COLLECTION} for p in self.raindrop.created))

        self.bridge(state).run_pass()
        self.assertEqual(len(self.raindrop.created), 40, 'a repeated pass must create no bookmark again')

    def test_the_first_eligible_occurrence_wins_the_tag(self):
        now = time.time()
        shared = 'https://shared.test/article'
        self.subscribe('first', tag='First')
        self.subscribe('second', tag='Second')
        for ident in ('first', 'second'):
            self.publish(ident, [{'title': 'shared', 'link': shared, 'published': now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        for ident in ('first', 'second'):
            self.cutoff(state, ident, now - HOUR)
        bridge.run_pass()
        rows = state.rows('SELECT * FROM deliveries')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['tag'], 'First')

    def test_url_case_and_fragments_collapse_but_queries_do_not(self):
        now = time.time()
        self.subscribe('alpha')
        self.publish('alpha', [
            {'title': 'a', 'link': 'https://Pub.test/Article#top', 'published': now - 60},
            {'title': 'b', 'link': 'https://pub.test/Article', 'published': now - 50},
            {'title': 'c', 'link': 'https://pub.test/Article?ref=x', 'published': now - 40},
        ])
        state = self.state()
        bridge = self.bridge(state)
        self.cutoff(state, 'alpha', now - HOUR)
        bridge.run_pass()
        self.assertEqual(sorted(r['url'] for r in state.rows('SELECT url FROM deliveries')),
                         ['https://pub.test/Article', 'https://pub.test/Article?ref=x'])


class Health(Harness):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.subscribe('alpha')

    def test_pending_then_healthy_then_error_then_recovered(self):
        state = self.state()
        bridge = self.bridge(state)
        self.assertEqual(state.feed('alpha')['health'], 'pending')
        self.assertIsNone(state.feed('alpha')['attempted_at'])

        self.publish('alpha', [])
        bridge.run_pass()
        row = state.feed('alpha')
        self.assertEqual(row['health'], 'healthy')
        self.assertIsNone(row['error'])
        self.assertIsNotNone(row['success_at'])

        self.http.fail(f'{FEED_BASE}/alpha.xml', DeadlineExpired('slow'))
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'error')

        self.publish('alpha', [])
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'healthy')
        self.assertIsNone(state.feed('alpha')['error'])

    def test_404_and_410_are_gone_and_still_retried(self):
        state = self.state()
        bridge = self.bridge(state)
        for status in (404, 410):
            self.publish('alpha', [], status=status)
            bridge.run_pass()
            self.assertEqual(state.feed('alpha')['health'], 'gone')
            self.assertIn(str(status), state.feed('alpha')['error'])
        self.publish('alpha', [])
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'healthy')

    def test_a_quiet_feed_stays_healthy_with_no_latest_article(self):
        state = self.state()
        self.publish('alpha', [])
        self.bridge(state).run_pass()
        row = state.feed('alpha')
        self.assertEqual(row['health'], 'healthy')
        self.assertIsNone(row['latest_at'])

    def test_a_broken_feed_does_not_block_the_others(self):
        self.subscribe('beta')
        self.subscribe('gamma')
        self.http.fail(f'{FEED_BASE}/alpha.xml', requests.ConnectionError('down'))
        self.http.route(f'{FEED_BASE}/beta.xml', b'<html>not a feed</html>')
        self.publish('gamma', [{'title': 'g', 'link': 'https://g.test/1', 'published': self.now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        for ident in ('alpha', 'beta', 'gamma'):
            self.cutoff(state, ident, self.now - HOUR)
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'error')
        self.assertEqual(state.feed('beta')['health'], 'error')
        self.assertEqual(state.feed('gamma')['health'], 'healthy')
        self.assertEqual(len(self.raindrop.created), 1)

    def test_an_empty_body_marks_the_feed_without_aborting_the_pass(self):
        self.subscribe('beta')
        self.http.route(f'{FEED_BASE}/alpha.xml', b'')
        self.publish('beta', [{'title': 'b', 'link': 'https://b.test/1', 'published': self.now - 60}])
        state = self.state()
        bridge = self.bridge(state)
        self.cutoff(state, 'beta', self.now - HOUR)
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'error')
        self.assertEqual(len(self.raindrop.created), 1)

    def test_an_oversized_feed_body_is_an_error_not_a_crash(self):
        self.http.route(f'{FEED_BASE}/alpha.xml', b'x' * (self.settings.max_feed_bytes + 1))
        state = self.state()
        self.bridge(state).run_pass()
        self.assertEqual(state.feed('alpha')['health'], 'error')

    def test_the_latest_publication_time_only_moves_forward(self):
        state = self.state()
        bridge = self.bridge(state)
        self.publish('alpha', [{'title': 'a', 'link': 'https://a.test/1', 'published': self.now - 120}])
        bridge.run_pass()
        latest = state.feed('alpha')['latest_at']
        self.publish('alpha', [{'title': 'b', 'link': 'https://a.test/2', 'published': self.now - 600}])
        bridge.run_pass()
        self.assertEqual(state.feed('alpha')['latest_at'], latest)


class InvalidConfiguration(Harness):
    def test_a_broken_file_keeps_the_last_valid_list_and_reports_the_error(self):
        self.subscribe('alpha')
        self.publish('alpha', [])
        state = self.state()
        bridge = self.bridge(state)
        self.assertEqual([f['id'] for f in bridge.feeds], ['alpha'])

        self.feeds_file.write_text('[ broken')
        bridge.run_pass()
        self.assertEqual([f['id'] for f in bridge.feeds], ['alpha'])
        self.assertIn('feeds.json', bridge.config_error)
        self.assertEqual(state.feed('alpha')['health'], 'healthy')

        self.write_feeds([{'id': 'alpha', 'url': f'{FEED_BASE}/alpha.xml', 'tag': 'alpha'},
                          {'id': 'beta', 'url': f'{FEED_BASE}/beta.xml', 'tag': 'beta'}])
        self.publish('beta', [])
        bridge.run_pass()
        self.assertIsNone(bridge.config_error)
        self.assertEqual([f['id'] for f in bridge.feeds], ['alpha', 'beta'])

    def test_an_invalid_file_at_startup_leaves_ingestion_paused(self):
        self.feeds_file.write_text('{}')
        state = self.state()
        bridge = self.bridge(state)
        self.assertEqual(bridge.feeds, [])
        self.assertIsNotNone(bridge.config_error)
        bridge.run_pass()
        self.assertEqual(state.rows('SELECT * FROM feeds'), [])

    def test_a_manual_edit_loads_on_the_next_pass(self):
        state = self.state()
        bridge = self.bridge(state)
        self.subscribe('added-by-hand')
        self.publish('added-by-hand', [])
        bridge.run_pass()
        self.assertEqual(state.feed('added-by-hand')['health'], 'healthy')
