"""Parser for Tutti.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class TuttiParser(BaseEmailParser):
    source = "tutti.ch"
    sender_patterns = (
        re.compile(r"[@.]tutti\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("tutti.ch",)
    # Tutti detail pages: /<locale>/a/<digits> (current shape) or the legacy
    # /<locale>/vi/<region>/immobilien/.../<digits>.
    listing_url_pattern = re.compile(
        r"tutti\.ch/(?:de|fr|it|en)/(?:a/\d+|vi/[^\s\"'>]+/\d+)",
        re.IGNORECASE,
    )
