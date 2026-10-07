"""SQLite state: cutoffs, feed health, the delivery queue, and incident memory."""

import fcntl
import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = '''
CREATE TABLE IF NOT EXISTS feeds (
 id TEXT PRIMARY KEY, added_at REAL NOT NULL, attempted_at REAL,
 success_at REAL, latest_at REAL, health TEXT NOT NULL DEFAULT 'pending',
 error TEXT, missing_dates INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 tag TEXT NOT NULL, destination INTEGER NOT NULL, feed_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','saved','needs_review')),
 bookmark_id INTEGER, error TEXT, retry_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS deliveries_state ON deliveries(state, retry_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''

INTERRUPTED = 'Interrupted submission; inspect Raindrop before retrying'


@contextmanager
def writer_lock(data_dir):
    """One container owns the data directory; a second writer is refused."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / 'writer.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise RuntimeError('Another bridge process owns this data directory; stop it first') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class State:
    """Serialized SQLite access. Ingestion and the GUI thread share one connection."""

    def __init__(self, path, dry_run=False):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(':memory:' if dry_run else path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if dry_run and path.exists():
            # Read-only clone into RAM; no schema or recovery write reaches production.
            source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            try:
                source.backup(self.db)
            finally:
                source.close()
        self.db.executescript(SCHEMA)
        self.db.commit()
        # A crash while submitting leaves a `sending` row of unknown remote outcome.
        self.execute("UPDATE deliveries SET state='needs_review', error=? WHERE state='sending'", (INTERRUPTED,))

    def close(self):
        with self.lock:
            self.db.close()

    def execute(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args).lastrowid

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def register(self, feeds, now):
        """Record each new feed's cutoff before any network fetch; never reset it."""
        with self.lock, self.db:
            self.db.executemany('INSERT OR IGNORE INTO feeds(id,added_at) VALUES (?,?)',
                                [(feed['id'], now) for feed in feeds])

    def feed(self, ident):
        rows = self.rows('SELECT * FROM feeds WHERE id=?', (ident,))
        return rows[0] if rows else None

    def feed_map(self):
        return {row['id']: row for row in self.rows('SELECT * FROM feeds')}

    def known_feed_ids(self):
        """Every feed ID ever registered, including removed ones that keep a cutoff."""
        return {row['id'] for row in self.rows('SELECT id FROM feeds')}

    def queue(self, articles):
        """Persist candidates before sending. The URL unique index is the
        cross-feed duplicate history and survives feed removal."""
        with self.lock, self.db:
            self.db.executemany('''INSERT OR IGNORE INTO deliveries
                (url,title,tag,destination,feed_id) VALUES (?,?,?,?,?)''', articles)

    def set_delivery(self, ident, state, error=None, bookmark_id=None, retry_at=0):
        self.execute('UPDATE deliveries SET state=?,error=?,bookmark_id=?,retry_at=? WHERE id=?',
                     (state, error, bookmark_id, retry_at, ident))

    def counts(self):
        counts = dict.fromkeys(('pending', 'sending', 'saved', 'needs_review'), 0)
        counts.update({row['state']: row['n']
                       for row in self.rows('SELECT state,count(*) n FROM deliveries GROUP BY state')})
        return counts

    def get_meta(self, key, default=None):
        rows = self.rows('SELECT value FROM meta WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    def set_meta(self, key, value):
        self.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, json.dumps(value)))
