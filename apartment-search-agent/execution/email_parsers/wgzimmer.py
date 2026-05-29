"""Parser for WGZimmer.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class WGZimmerParser(BaseEmailParser):
    source = "wgzimmer.ch"
    sender_patterns = (
        re.compile(r"[@.]wgzimmer\.ch\b", re.IGNORECASE),
        re.compile(r"[@.]wgzimmer\.com\b", re.IGNORECASE),
    )
    imap_search_domains = ("wgzimmer.ch", "wgzimmer.com")
    # WGZimmer ad pages look like /wgzimmer/search/mate/ad/<uuid>.html
    listing_url_pattern = re.compile(
        r"wgzimmer\.ch/wgzimmer/(?:search/)?(?:mate|wohnung)/(?:ad/)?[^\s\"'>]+",
        re.IGNORECASE,
    )
