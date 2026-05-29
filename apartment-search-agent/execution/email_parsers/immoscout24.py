"""Parser for ImmoScout24.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class ImmoScout24Parser(BaseEmailParser):
    source = "immoscout24.ch"
    sender_patterns = (
        re.compile(r"[@.]immoscout24\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("immoscout24.ch",)
    # Detail pages end with a numeric id. Require it so the alert footer's
    # /de/d/wohnungen-mieten/luzern (search page) doesn't match.
    listing_url_pattern = re.compile(
        r"immoscout24\.ch/(?:(?:Miet|Kauf)objekt/\d+|(?:de|fr|it|en)/d/[a-z0-9-]+/\d+)",
        re.IGNORECASE,
    )
