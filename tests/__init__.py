"""Keep the suite output readable; the bridge logs warnings by design."""

import logging

logging.getLogger('raindrop_rss').addHandler(logging.NullHandler())
logging.getLogger('raindrop_rss').propagate = False
