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
    # Detail pages end with the numeric listing id. Require it so /rent (the
    # alert footer "all results" link) and similar landing pages don't match.
    listing_url_pattern = re.compile(
        r"homegate\.ch/(?:rent|mieten|buy|kaufen)/\d+",
        re.IGNORECASE,
    )
