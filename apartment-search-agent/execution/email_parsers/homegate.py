"""Parser for Homegate.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class HomegateParser(BaseEmailParser):
    source = "homegate.ch"
    sender_patterns = (
        re.compile(r"[@.]homegate\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("homegate.ch",)
    # Homegate listing detail pages: /rent/<id> or /mieten/<id> or /buy/<id>.
    listing_url_pattern = re.compile(
        r"homegate\.ch/(?:rent|mieten|buy|kaufen)/[^\s\"'>]+",
        re.IGNORECASE,
    )
