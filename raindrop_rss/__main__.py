"""Entry point: the hourly service, a single pass, a dry run, and review repair."""

import argparse
import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path

from . import gui
from .bridge import Bridge
from .config import ConfigError, Settings
from .state import State, writer_lock

log = logging.getLogger('raindrop_rss')
DEFAULT_CONFIG = os.environ.get('RAINDROP_RSS_CONFIG', 'config/settings.toml')


def parser():
    root = argparse.ArgumentParser(prog='raindrop-rss', description=__doc__)
    root.add_argument('--config', default=DEFAULT_CONFIG, type=Path, help='settings TOML (default %(default)s)')
    root.add_argument('--once', action='store_true', help='run one pass and exit instead of serving hourly')
    root.add_argument('--dry-run', action='store_true',
                      help='preview one pass: no listener, no state or Raindrop changes')
    root.add_argument('--debug', action='store_true', help='verbose logging')
    commands = root.add_subparsers(dest='command')
    review = commands.add_parser('review', help='resolve one delivery left in needs_review')
    actions = review.add_subparsers(dest='action', required=True)
    actions.add_parser('list', help='show deliveries awaiting manual review')
    saved = actions.add_parser('saved', help='the bookmark exists in Raindrop: record its ID')
    saved.add_argument('row', type=positive)
    saved.add_argument('bookmark_id', type=positive)
    requeue = actions.add_parser('requeue', help='the bookmark is absent from Raindrop: send it again')
    requeue.add_argument('row', type=positive)
    return root


def positive(text):
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def review(state, args):
    if args.action == 'list':
        rows = state.rows("SELECT * FROM deliveries WHERE state='needs_review' ORDER BY id")
        if not rows:
            print('Nothing needs review.')
            return 0
        print('Search Raindrop for each URL, then run: review saved <row> <bookmark-id> | review requeue <row>')
        for row in rows:
            print(f'\nrow {row["id"]}  feed {row["feed_id"]}  tag {row["tag"]}')
            print(f'  url    {row["url"]}')
            print(f'  title  {row["title"] or "(none)"}')
            print(f'  reason {row["error"] or "(none)"}')
            if row['bookmark_id']:
                print(f'  bookmark reported by Raindrop: {row["bookmark_id"]}')
        return 0
    rows = state.rows("SELECT id FROM deliveries WHERE id=? AND state='needs_review'", (args.row,))
    if not rows:
        print(f'No delivery {args.row} is awaiting review.', file=sys.stderr)
        return 1
    if args.action == 'saved':
        state.set_delivery(args.row, 'saved', bookmark_id=args.bookmark_id)
        print(f'Row {args.row} recorded as saved with bookmark {args.bookmark_id}.')
    else:
        state.set_delivery(args.row, 'pending')
        print(f'Row {args.row} requeued; it will be sent on the next pass.')
    return 0


def configure_logging(debug):
    """Verbose means verbose about us, not about urllib3.

    Root-level DEBUG makes urllib3 log every request line, which would put feed
    URL query values -- some of them private tokens -- straight into the log.
    """
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger().setLevel(logging.WARNING)
    log.setLevel(logging.DEBUG if debug else logging.INFO)


def serve_forever(settings, bridge):
    """Run the listener beside the loop, and close it before state does."""
    server = gui.serve(bridge, settings)
    try:
        bridge.run()
    finally:
        server.shutdown()
        server.server_close()
    log.info('Stopped.')


def main(argv=None):
    args = parser().parse_args(argv)
    configure_logging(args.debug)
    if args.dry_run and args.command == 'review':
        log.error('--dry-run cannot be combined with review; review writes to the database by design')
        return 2
    try:
        settings = Settings.load(args.config)
    except ConfigError as exc:
        log.error('%s', exc)
        return 2

    try:
        if args.dry_run:
            # No lock, no listener, no production file: the clone lives in RAM.
            state = State(settings.data_dir / 'state.db', dry_run=True)
            try:
                Bridge(settings, state, dry_run=True).run_pass()
            finally:
                state.close()
            log.info('Dry run complete; no state or Raindrop change was made.')
            return 0

        with writer_lock(settings.data_dir):
            state = State(settings.data_dir / 'state.db')
            try:
                if args.command == 'review':
                    return review(state, args)
                bridge = Bridge(settings, state)
                for name in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(name, lambda *_: bridge.shutdown())
                if args.once:
                    bridge.run_pass()
                else:
                    serve_forever(settings, bridge)
            finally:
                state.close()
    except RuntimeError as exc:
        log.error('%s', exc)
        return 3
    except OSError as exc:
        log.error('Cannot use the data directory or GUI port: %s', exc)
        return 4
    except sqlite3.Error:
        # The message can carry file paths; the traceback goes to debug only.
        log.error('The state database is unusable; check %s', settings.data_dir / 'state.db')
        log.debug('database failure', exc_info=True)
        return 5
    return 0


if __name__ == '__main__':
    sys.exit(main())
