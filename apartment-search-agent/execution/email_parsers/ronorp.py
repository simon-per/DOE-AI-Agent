"""Parser for Ronorp.net classifieds saved-search alert emails."""

from __future__ import annotations

import re

from .base import BaseEmailParser


class RonorpParser(BaseEmailParser):
    source = "ronorp.net"
    sender_patterns = (
        re.compile(r"[@.]ronorp\.net\b", re.IGNORECASE),
        re.compile(r"[@.]ronorp\.ch\b", re.IGNORECASE),
    )
    imap_search_domains = ("ronorp.net", "ronorp.ch")
    # Detail pages: /<region>/ronorp/inserate/<category>/<slug>-<digits>
    # or the shortened /p/<digits>. The trailing numeric id is required —
    # search-results landings end at the category level without one.
    listing_url_pattern = re.compile(
        r"ronorp\.(?:net|ch)/(?:[a-z]+/ronorp/inserate/[^\s\"'>]*?-\d+|p/\d+)",
        re.IGNORECASE,
    )
