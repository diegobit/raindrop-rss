"""Outside world: bounded HTTP, the Raindrop API, and the `tg` notifier."""

import json
import logging
import math
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import requests
from urllib3.exceptions import NewConnectionError

log = logging.getLogger(__name__)
API = 'https://api.raindrop.io/rest/v1'


class DeadlineExpired(Exception):
    pass


@contextmanager
def deadline(seconds):
    """Bound DNS, connect, redirects and slow-drip bodies on Linux and macOS.

    Ingestion runs in the main thread, the only place SIGALRM can be armed; the
    GUI thread falls back to the socket timeouts alone.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def expired(signum, frame):
        raise DeadlineExpired('Request time limit reached')

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    url: str

    def json(self):
        result = json.loads(self.body)
        if not isinstance(result, dict):
            raise ValueError('Expected an object')
        return result


class Budget:
    """Bounds every response body, redirect hops included.

    requests reads an intermediate response in full before following it -- and
    does so even with allow_redirects=False, to prepare `_next` -- so a size
    check on the final response alone lets a huge redirect body through. As a
    response hook this runs first, on each hop, and caches what it read so
    requests' own consumption is a no-op. The limit is cumulative across hops.
    """

    def __init__(self, max_bytes):
        self.remaining = max_bytes

    def __call__(self, response, *args, **kwargs):
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.remaining:
                    raise ValueError('Response exceeds size limit')
                chunks.append(chunk)
        except BaseException:
            response.close()
            raise
        self.remaining -= size
        response._content = b''.join(chunks)
        response._content_consumed = True
        return response


class HTTP:
    def request(self, method, url, seconds, max_bytes, **kwargs):
        # One absolute deadline covers the whole chain. Redirects are followed
        # only for GET, and requests strips credentials when the host changes.
        with deadline(seconds):
            with requests.request(method, url, timeout=(min(5, seconds), seconds),
                                  stream=True, allow_redirects=method == 'GET',
                                  hooks={'response': Budget(max_bytes)}, **kwargs) as response:
                return Response(response.status_code, dict(response.headers),
                                response.content, response.url)


def finite(value):
    """A usable absolute timestamp, or None for a malformed or nonfinite header."""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def retry_time(headers, now):
    """Honour Retry-After and X-RateLimit-Reset (UTC epoch).

    A server asking for longer than our hourly interval gets it; only malformed
    or nonfinite values are discarded, never shortened.
    """
    headers = {key.lower(): value for key, value in headers.items()}
    retry = now
    value = headers.get('retry-after', '')
    delay = finite(value)
    if delay is not None:
        retry = max(retry, now + delay)
    else:
        try:
            moment = parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, OverflowError):
            moment = None
        if moment is not None and math.isfinite(moment):
            retry = max(retry, moment)
    reset = finite(headers.get('x-ratelimit-reset'))
    if reset is not None:
        retry = max(retry, reset)
    return retry


def never_submitted(error):
    """True only when the request provably never reached Raindrop.

    requests wraps a refused connection or a DNS failure as
    ConnectionError(MaxRetryError(reason=NewConnectionError)). A reset or a
    truncated read arrives as ConnectionError(ProtocolError) with no `reason`,
    and stays ambiguous because the server may already have committed.
    """
    if isinstance(error, requests.ConnectTimeout):
        return True
    for _ in range(5):
        if isinstance(error, NewConnectionError):
            return True
        inner = getattr(error, 'reason', None)
        if inner is None and getattr(error, 'args', None) and isinstance(error.args[0], BaseException):
            inner = error.args[0]
        if inner is None:
            return False
        error = inner
    return False


@dataclass
class Result:
    state: str
    error: str | None = None
    bookmark_id: int | None = None
    retry_at: float = 0
    halt: bool = False


class Raindrop:
    def __init__(self, http, token):
        self.http = http
        self.headers = {'Authorization': 'Bearer ' + token}

    def check_collection(self, ident, seconds):
        """An invalid destination must error, never silently fall back to Unsorted."""
        try:
            response = self.http.request('GET', f'{API}/collection/{ident}', seconds, 1024 * 1024,
                                         headers=self.headers)
        except (requests.RequestException, DeadlineExpired, ValueError):
            return Result('pending', 'Destination check failed', halt=True)
        if response.status == 429:
            return Result('pending', 'Raindrop rate limit',
                          retry_at=retry_time(response.headers, time.time()), halt=True)
        if response.status in (401, 403):
            return Result('pending', 'Raindrop credentials or permissions rejected', halt=True)
        try:
            data = response.json()
            item = data.get('item')
            if response.status == 200 and data.get('result') is True and isinstance(item, dict) and item.get('_id') == ident:
                access = item.get('access', {})
                if isinstance(access, dict) and access.get('level', 4) < 3:
                    return Result('pending', 'Destination is not writable', halt=True)
                return Result('ok')
        except (ValueError, TypeError):
            pass
        return Result('pending', 'Destination missing, invalid or unavailable', halt=True)

    def create(self, row, seconds):
        payload = {'link': row['url'], 'collection': {'$id': row['destination']}, 'tags': [row['tag']]}
        if row['title']:
            payload['title'] = row['title']
        try:
            response = self.http.request('POST', API + '/raindrop', seconds, 1024 * 1024,
                                         headers=self.headers, json=payload)
        except (requests.RequestException, DeadlineExpired, ValueError) as exc:
            if never_submitted(exc):
                return Result('pending', 'Could not reach Raindrop; nothing was submitted')
            # The request may already have committed remotely. Never resend blindly.
            return Result('needs_review', 'Submission outcome unknown; inspect Raindrop before retrying')
        if response.status in (401, 403):
            return Result('pending', 'Raindrop credentials or permissions rejected', halt=True)
        if response.status == 429:
            return Result('pending', 'Raindrop rate limit',
                          retry_at=retry_time(response.headers, time.time()), halt=True)
        if response.status == 404:
            return Result('pending', 'Destination missing or API unavailable', halt=True)
        if response.status in (400, 422):
            return Result('needs_review', 'Article payload rejected')
        try:
            data = response.json()
            item = data.get('item')
            if 200 <= response.status < 300 and data.get('result') is True and isinstance(item, dict):
                ident = item.get('_id')
                if type(ident) is int and ident > 0:
                    # A remote fallback to Unsorted is a review issue, not a success.
                    collection = item.get('collection')
                    if not isinstance(collection, dict) or collection.get('$id') != row['destination']:
                        return Result('needs_review', 'Created bookmark destination unconfirmed; inspect Raindrop',
                                      bookmark_id=ident, halt=True)
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    exhausted = headers.get('ratelimit-remaining', headers.get('x-ratelimit-remaining')) == '0'
                    return Result('saved', bookmark_id=ident,
                                  retry_at=retry_time(headers, time.time()) if exhausted else 0, halt=exhausted)
        except (ValueError, TypeError):
            pass
        # Even a 5xx can follow a successful remote commit.
        return Result('needs_review', 'Ambiguous Raindrop response; inspect before retrying')


def notify(command, message, timeout=20):
    """Send one message as a single argv element; never through a shell."""
    try:
        subprocess.run([*command, message], check=True, timeout=timeout,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        log.warning('Alert command %s is not available; saving continues', command[0] if command else 'tg')
        return False
    except (OSError, subprocess.SubprocessError):
        log.warning('Alert command failed; will retry next pass')
        return False
