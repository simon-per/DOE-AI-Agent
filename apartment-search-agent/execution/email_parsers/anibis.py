"""Parser for Anibis.ch saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class AnibisParser(BaseEmailParser):
    source = "anibis.ch"
    sender_patterns = (
        re.compile(r"[@.]anibis\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("anibis.ch",)
    # Detail pages follow /<locale>/d/<slug>-<digits> (the trailing -id token
    # is what distinguishes a real listing from the /c/category landing page).
    listing_url_pattern = re.compile(
        r"anibis\.ch/(?:de|fr|it|en)/d/[^\s\"'>]*?-\d+",
        re.IGNORECASE,
    )
