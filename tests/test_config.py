"""Settings, feeds.json validation, and atomic subscription writes."""

import json
import os
import stat
import unittest

from raindrop_rss.config import (ConfigError, Settings, add_feed, display_url,
                                 generate_id, normalize_url, read_feeds, validate_feeds)

from .support import PROJECT, Harness


class NormalizeURL(unittest.TestCase):
    def test_lowercases_scheme_and_host_only(self):
        self.assertEqual(normalize_url('HTTPS://Example.COM/Path/A?B=C'),
                         'https://example.com/Path/A?B=C')

    def test_drops_the_fragment_but_keeps_the_query(self):
        self.assertEqual(normalize_url('https://a.test/x?q=1#frag'), 'https://a.test/x?q=1')

    def test_query_variants_stay_distinct_bookmarks(self):
        self.assertNotEqual(normalize_url('https://a.test/x'), normalize_url('https://a.test/x?utm=1'))

    def test_rejects_non_http_credentials_and_whitespace(self):
        for value in ('ftp://a.test/x', 'javascript:alert(1)', 'https://u:p@a.test/x',
                      'https:// a.test/x', '', 'https:///x', None, 'https://a.test:99999/x'):
            with self.assertRaises(ConfigError):
                normalize_url(value)

    def test_display_url_redacts_query_values(self):
        self.assertEqual(display_url('https://a.test/feed?token=secret'), 'https://a.test/feed?redacted')
        self.assertNotIn('secret', display_url('https://a.test/feed?token=secret'))


class FeedFile(unittest.TestCase):
    def test_requires_exactly_the_three_fields(self):
        with self.assertRaises(ConfigError):
            validate_feeds([{'id': 'a', 'url': 'https://a.test/f', 'tag': 'a', 'health': 'healthy'}])
        with self.assertRaises(ConfigError):
            validate_feeds([{'id': 'a', 'url': 'https://a.test/f'}])

    def test_rejects_duplicate_id_or_url(self):
        base = {'id': 'a', 'url': 'https://a.test/f', 'tag': 'a'}
        with self.assertRaises(ConfigError):
            validate_feeds([base, dict(base, url='https://b.test/f')])
        with self.assertRaises(ConfigError):
            validate_feeds([base, dict(base, id='b')])

    def test_rejects_duplicate_url_differing_only_in_case(self):
        with self.assertRaises(ConfigError):
            validate_feeds([{'id': 'a', 'url': 'https://A.test/f', 'tag': 'a'},
                            {'id': 'b', 'url': 'https://a.test/f', 'tag': 'b'}])

    def test_rejects_bad_ids_and_tags(self):
        for bad in ({'id': 'has space', 'url': 'https://a.test/f', 'tag': 'a'},
                    {'id': 'a', 'url': 'https://a.test/f', 'tag': '  '},
                    {'id': 'a', 'url': 'https://a.test/f', 'tag': 'x\ny'},
                    {'id': '', 'url': 'https://a.test/f', 'tag': 'a'}):
            with self.assertRaises(ConfigError):
                validate_feeds([bad])

    def test_generate_id_slugifies_and_deduplicates(self):
        self.assertEqual(generate_id('Hacker News', set()), 'hacker-news')
        self.assertEqual(generate_id('Hacker News', {'hacker-news'}), 'hacker-news-2')
        self.assertEqual(generate_id('***', {'feed'}), 'feed-2')


class AtomicAdd(Harness):
    def setUp(self):
        super().setUp()
        self.subscribe('one')
        self.feeds, self.revision = read_feeds(self.feeds_file)

    def test_appends_and_preserves_file_order(self):
        feeds = add_feed(self.feeds_file, self.revision, 'https://b.test/f', 'beta')
        self.assertEqual([f['id'] for f in feeds], ['one', 'beta'])
        self.assertEqual(json.loads(self.feeds_file.read_text()), feeds)

    def test_export_carries_no_runtime_status(self):
        add_feed(self.feeds_file, self.revision, 'https://b.test/f', 'beta')
        for entry in json.loads(self.feeds_file.read_text()):
            self.assertEqual(set(entry), {'id', 'url', 'tag'})

    def test_rejects_a_conflicting_manual_edit_without_overwriting(self):
        self.subscribe('manual')
        before = self.feeds_file.read_text()
        with self.assertRaises(ConfigError):
            add_feed(self.feeds_file, self.revision, 'https://b.test/f', 'beta')
        self.assertEqual(self.feeds_file.read_text(), before)

    def test_rejects_a_duplicate_url(self):
        with self.assertRaises(ConfigError):
            add_feed(self.feeds_file, self.revision, self.feeds[0]['url'], 'other')

    def test_reports_a_write_failure_and_leaves_no_debris(self):
        before = self.feeds_file.read_text()
        directory = self.feeds_file.parent
        mode = directory.stat().st_mode
        os.chmod(directory, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with self.assertRaises(ConfigError):
                add_feed(self.feeds_file, self.revision, 'https://b.test/f', 'beta')
        finally:
            os.chmod(directory, mode)
        self.assertEqual(self.feeds_file.read_text(), before)
        self.assertEqual([p.name for p in directory.iterdir()], ['feeds.json'])

    def test_invalid_json_is_a_config_error(self):
        self.feeds_file.write_text('{not json')
        with self.assertRaises(ConfigError):
            read_feeds(self.feeds_file)

    def test_missing_file_is_a_config_error(self):
        self.feeds_file.unlink()
        with self.assertRaises(ConfigError):
            read_feeds(self.feeds_file)

    def test_oversized_file_is_a_config_error(self):
        self.feeds_file.write_text('[' + '0,' * 700000 + '0]')
        with self.assertRaises(ConfigError):
            read_feeds(self.feeds_file)


class SettingsFile(Harness):
    def write(self, text):
        path = self.root / 'config' / 'settings.toml'
        path.write_text(text)
        return path

    def minimal(self, extra=''):
        return self.write('collection_id = 7\nfeeds_file = "../subscriptions/feeds.json"\n'
                          'data_dir = "../data"\ntoken_file = "raindrop-token"\n' + extra)

    def test_resolves_paths_relative_to_the_settings_file(self):
        settings = Settings.load(self.minimal())
        self.assertEqual(settings.feeds_file, self.feeds_file)
        self.assertEqual(settings.token_file, self.token_file)
        self.assertEqual(settings.port, 38471)
        self.assertEqual(settings.tg_command, ('tg', '--'))

    def test_accepts_an_explicit_telegram_profile(self):
        settings = Settings.load(self.minimal('tg_command = ["tg", "-p", "alerts"]\n'))
        self.assertEqual(settings.tg_command, ('tg', '-p', 'alerts'))

    def test_rejects_unknown_missing_and_malformed_settings(self):
        for text in ('collection_id = 7\n',
                     'collection_id = 7\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\nnope=1\n',
                     'collection_id = "7"\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\n',
                     'collection_id = 7\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\nport=70000\n',
                     'collection_id = 7\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\ntg_command=[]\n',
                     'collection_id = 7\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\ntg_command="tg"\n',
                     'collection_id = 7\nfeeds_file="a"\ndata_dir="b"\ntoken_file="c"\nbind=""\n',
                     'not toml ['):
            with self.assertRaises(ConfigError):
                Settings.load(self.write(text))

    def test_missing_settings_file_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            Settings.load(self.root / 'config' / 'absent.toml')

    def test_the_shipped_example_names_a_token_file_and_mentions_the_alert_command(self):
        body = (PROJECT / 'config' / 'settings.example.toml').read_text()
        self.assertIn('token_file', body)
        self.assertIn('tg_command', body)
        self.assertNotIn('Bearer', body)
