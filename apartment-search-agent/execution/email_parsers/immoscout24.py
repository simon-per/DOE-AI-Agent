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
    # ImmoScout24 detail pages: /Mietobjekt/<id> or /de/d/wohnung-mieten/...
    listing_url_pattern = re.compile(
        r"immoscout24\.ch/(?:Mietobjekt|Kaufobjekt|de/d|fr/d|it/d|en/d)/[^\s\"'>]+",
        re.IGNORECASE,
    )
