"""Settings file, subscription file, and the URL rules shared by both."""

import hashlib
import json
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

FEED_FILE_LIMIT = 1024 * 1024


class ConfigError(ValueError):
    """A human-readable problem with operator-supplied configuration."""


def normalize_url(value):
    """Return the deduplication key for an article or feed URL.

    Scheme and host are lowercased and the fragment dropped; path case and
    query stay untouched, so variants remain distinct bookmarks.
    """
    if not isinstance(value, str) or not value or re.search(r'\s', value):
        raise ConfigError('URL must be an HTTP(S) URL without whitespace')
    if len(value) > 2000:
        raise ConfigError('URL is too long')
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            raise ValueError()
        if parts.username is not None or parts.password is not None:
            raise ValueError()
        host = parts.hostname.lower()
        if ':' in host:
            host = '[' + host + ']'
        if parts.port is not None:
            host += ':' + str(parts.port)
        return urlunsplit((parts.scheme.lower(), host, parts.path, parts.query, ''))
    except ValueError:
        raise ConfigError('URL must be HTTP(S), with a valid host/port and no embedded credentials') from None


def display_url(value):
    """Never expose URL query values or credentials in status or log output."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parts.port is not None:
            host += ':' + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, 'redacted' if parts.query else '', ''))
    except ValueError:
        return '[invalid URL]'


def validate_feeds(raw):
    if not isinstance(raw, list):
        raise ConfigError('feeds.json must contain an array')
    ids, urls = set(), set()
    result = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {'id', 'url', 'tag'}:
            raise ConfigError('Each feed must contain exactly id, url and tag')
        ident, tag = entry['id'], entry['tag']
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident):
            raise ConfigError('Feed IDs must use 1-100 letters, digits, underscores or hyphens')
        if not isinstance(tag, str) or not tag.strip() or len(tag) > 100 or any(ord(c) < 32 for c in tag):
            raise ConfigError('Source tag must be 1-100 printable characters')
        url = normalize_url(entry['url'])
        if ident in ids or url in urls:
            raise ConfigError('Duplicate feed ID or URL')
        ids.add(ident)
        urls.add(url)
        result.append({'id': ident, 'url': url, 'tag': tag.strip()})
    return result


def read_feeds(path):
    """Return (feeds, revision). The revision detects concurrent manual edits."""
    try:
        with path.open('rb') as handle:
            data = handle.read(FEED_FILE_LIMIT + 1)
        if len(data) > FEED_FILE_LIMIT:
            raise ConfigError('Feed file exceeds 1 MiB')
        feeds = validate_feeds(json.loads(data))
        return feeds, hashlib.sha256(data).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError('Cannot read feeds.json: check its permissions and UTF-8 JSON syntax') from None


def generate_id(tag, taken):
    """Readable, stable ID derived from the tag; feeds.json stays hand-editable."""
    base = re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', tag.lower())).strip('-')[:60] or 'feed'
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f'{base}-{suffix}'
    return candidate


def add_feed(path, revision, url, tag, known_ids=()):
    """Append one feed with an atomic replacement, refusing conflicting edits.

    `known_ids` carries every feed ID SQLite has ever seen. A generated ID must
    avoid those too: reusing a retired ID would inherit its cutoff and make a
    different publisher's back catalogue look already-imported. Re-adding a feed
    by writing its old ID into the file by hand still keeps that history.
    """
    feeds, current = read_feeds(path)
    if current != revision:
        raise ConfigError('Feed file changed since the page loaded; reload and try again')
    taken = {existing['id'] for existing in feeds} | set(known_ids)
    feeds = validate_feeds(feeds + [{'id': generate_id(tag, taken), 'url': url, 'tag': tag}])
    temp = None
    try:
        # Keep the operator's permissions: the file may hold private query values.
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            json.dump(feeds, out, ensure_ascii=False, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        # Detect edits made while preparing the replacement. External editors must
        # also use atomic replacement; stop the service for guaranteed exclusion.
        if read_feeds(path)[1] != current:
            raise ConfigError('Feed file changed while saving; reload and try again')
        os.chmod(temp, mode)
        os.replace(temp, path)
        temp = None
    except OSError:
        raise ConfigError('Could not save feeds.json; check directory permissions') from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return feeds


@dataclass(frozen=True)
class Settings:
    collection_id: int
    feeds_file: Path
    data_dir: Path
    token_file: Path
    port: int = 38471
    bind: str = '127.0.0.1'
    interval_seconds: int = 3600
    request_timeout: int = 15
    pass_seconds: int = 300
    max_feed_bytes: int = 2 * 1024 * 1024
    notify_timeout: int = 20
    tg_command: tuple = ('tg', '--')

    @classmethod
    def load(cls, path):
        try:
            raw = tomllib.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            raise ConfigError('Cannot read settings: check file permissions and TOML syntax') from None
        if unknown := set(raw) - set(cls.__dataclass_fields__):
            raise ConfigError('Unknown setting ' + sorted(unknown)[0] + '; see config/settings.example.toml')
        for name in ('collection_id', 'feeds_file', 'data_dir', 'token_file'):
            if name not in raw:
                raise ConfigError('Missing required setting: ' + name)
        for name in ('collection_id', 'port', 'interval_seconds', 'request_timeout',
                     'pass_seconds', 'max_feed_bytes', 'notify_timeout'):
            if name in raw and (type(raw[name]) is not int or raw[name] <= 0):
                raise ConfigError(name + ' must be a positive integer')
        if raw.get('port', 38471) > 65535:
            raise ConfigError('port must be at most 65535')
        if 'bind' in raw and (not isinstance(raw['bind'], str) or not raw['bind']):
            raise ConfigError('bind must be a nonempty address string')
        for name in ('feeds_file', 'data_dir', 'token_file'):
            if not isinstance(raw[name], str) or not raw[name]:
                raise ConfigError(name + ' must be a path')
            raw[name] = (path.parent / raw[name]).resolve()
        command = raw.get('tg_command', ['tg', '--'])
        if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
            raise ConfigError('tg_command must be a nonempty array of command arguments')
        raw['tg_command'] = tuple(command)
        return cls(**raw)
