"""Parser for Newhome.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class NewhomeParser(BaseEmailParser):
    source = "newhome.ch"
    sender_patterns = (
        re.compile(r"[@.]newhome\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("newhome.ch",)
    # Detail pages always carry the id-<digits> token. Anchor on that so the
    # /suchen and /agentur footer links in every alert don't false-positive.
    listing_url_pattern = re.compile(
        r"newhome\.ch/(?:de|fr|it|en)/(?:mieten|kaufen)/[^\s\"'>]*?id-\d+",
        re.IGNORECASE,
    )
