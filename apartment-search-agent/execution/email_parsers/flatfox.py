"""Parser for Flatfox.ch alert + reply emails.

Even though the public API is the primary Flatfox source, alert and reply
emails surface listings that the API may not yet have indexed and let us
track conversations against the same canonical URL.
"""

from __future__ import annotations

import re

from .base import BaseEmailParser


class FlatfoxParser(BaseEmailParser):
    source = "flatfox.ch"
    sender_patterns = (
        # Match any flatfox.ch subdomain in the From address (covers
        # user.flatfox.ch, priority.flatfox.ch, and bare flatfox.ch).
        re.compile(r"[@.]flatfox\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("flatfox.ch",)
    # Flatfox detail pages: /flat/<slug>/<id>/ or /en/flat/... or /de/flat/...
    listing_url_pattern = re.compile(
        r"flatfox\.ch/(?:[a-z]{2}/)?flat/[^\s\"'>]+",
        re.IGNORECASE,
    )
