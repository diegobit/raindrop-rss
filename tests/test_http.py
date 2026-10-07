"""Bounded reads and the absolute deadline, against a real local server."""

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from raindrop_rss.adapters import HTTP, DeadlineExpired


class Publisher(BaseHTTPRequestHandler):
    """Redirects, oversized bodies, and a slow drip. Counts POSTs."""

    protocol_version = 'HTTP/1.1'
    posts = 0

    def log_message(self, *args):
        pass

    def reply(self, status, body=b'', location=None):
        self.send_response(status)
        if location:
            self.send_header('Location', location)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/fat-redirect':
            self.reply(302, b'x' * 65536, '/end')
        elif self.path == '/thin-redirect':
            self.reply(302, b'moved', '/end')
        elif self.path == '/chained':
            self.reply(302, b'y' * 600, '/thin-redirect')
        elif self.path == '/fat-final':
            self.reply(200, b'z' * 65536)
        elif self.path == '/drip':
            # Chunked, one chunk every 200 ms: each read beats the socket
            # timeout, so only the absolute deadline can stop it.
            self.send_response(200)
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b'4\r\ndrip\r\n')
                    self.wfile.flush()
                    time.sleep(0.2)
                self.wfile.write(b'0\r\n\r\n')
            except OSError:
                pass
        else:
            self.reply(200, b'ok')

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        self.rfile.read(length)
        type(self).posts += 1
        self.reply(302, b'p' * 4096, '/end')


class BoundedReads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Publisher)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:%d' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def setUp(self):
        Publisher.posts = 0

    def test_an_oversized_redirect_body_is_refused(self):
        """The 302 body is 65536 bytes; only the 2-byte final body used to be checked."""
        with self.assertRaises(ValueError):
            HTTP().request('GET', self.base + '/fat-redirect', 3, 1024)

    def test_a_small_redirect_still_succeeds(self):
        response = HTTP().request('GET', self.base + '/thin-redirect', 3, 1024)
        self.assertEqual((response.status, response.body), (200, b'ok'))
        self.assertTrue(response.url.endswith('/end'))

    def test_an_oversized_final_body_is_refused(self):
        with self.assertRaises(ValueError):
            HTTP().request('GET', self.base + '/fat-final', 3, 1024)

    def test_the_budget_is_cumulative_across_hops(self):
        """Two redirects then the body: 600 + 5 + 2 bytes fits in 1024, not in 602."""
        self.assertEqual(HTTP().request('GET', self.base + '/chained', 3, 1024).body, b'ok')
        with self.assertRaises(ValueError):
            HTTP().request('GET', self.base + '/chained', 3, 602)

    def test_a_body_exactly_at_the_limit_is_kept(self):
        self.assertEqual(HTTP().request('GET', self.base + '/end', 3, 2).body, b'ok')
        with self.assertRaises(ValueError):
            HTTP().request('GET', self.base + '/end', 3, 1)

    def test_the_absolute_deadline_stops_a_slow_drip(self):
        started = time.monotonic()
        with self.assertRaises(DeadlineExpired):
            HTTP().request('GET', self.base + '/drip', 1, 1024 * 1024)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_post_is_never_redirected_or_repeated(self):
        response = HTTP().request('POST', self.base + '/submit', 3, 8192, json={'a': 1})
        self.assertEqual(response.status, 302)
        self.assertEqual(Publisher.posts, 1, 'the redirect must not resend the POST')

    def test_an_oversized_post_response_is_refused_without_a_second_post(self):
        with self.assertRaises(ValueError):
            HTTP().request('POST', self.base + '/submit', 3, 1024, json={'a': 1})
        self.assertEqual(Publisher.posts, 1)


class AmbiguousPostRedirect(unittest.TestCase):
    """A 302 to a POST is not a success, so the row goes to review."""

    def test_a_redirected_submission_needs_review(self):
        from raindrop_rss.adapters import Raindrop
        from .support import FakeHTTP, Response

        http = FakeHTTP()
        http.routes['https://api.raindrop.io/rest/v1/raindrop'] = Response(
            302, {'Location': '/elsewhere'}, b'', 'https://api.raindrop.io/rest/v1/raindrop')
        result = Raindrop(http, 'token').create(
            {'url': 'https://a.test/1', 'title': 'x', 'tag': 'T', 'destination': 1}, 5)
        self.assertEqual(result.state, 'needs_review')
        self.assertEqual(len([c for c in http.calls if c['method'] == 'POST']), 1)
