"""Parser for Comparis.ch real-estate saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class ComparisParser(BaseEmailParser):
    source = "comparis.ch"
    sender_patterns = (
        re.compile(r"[@.]comparis\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("comparis.ch",)
    # Detail pages live under /immobilien/marktplatz/(details/)?(show/)?<id>.
    # Require the numeric id so the marketplace landing /immobilien/marktplatz
    # itself doesn't match every alert footer.
    listing_url_pattern = re.compile(
        r"comparis\.ch/immobilien/marktplatz/(?:details/)?(?:show/)?\d+",
        re.IGNORECASE,
    )
