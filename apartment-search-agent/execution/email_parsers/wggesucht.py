"""Parser for WG-Gesucht.de saved-search ("Suchauftrag") alert emails.

WG-Gesucht is a large flatshare platform with genuine Luzern inventory not
covered by the Swiss-only portals. We parse the alert email only — never the
site (it is bot-protected and login-walled for messaging).
"""

from __future__ import annotations

import re

from .base import BaseEmailParser


class WgGesuchtParser(BaseEmailParser):
    source = "wg-gesucht.de"
    sender_patterns = (
        re.compile(r"[@.]wg-gesucht\.de\b", re.IGNORECASE),
    )
    imap_search_domains = ("wg-gesucht.de",)
    # Detail pages end in `.<ad-id>.html`, where the ad id is a long numeric
    # run (7-8 digits), e.g. `.../wg-zimmer-in-Luzern-Guetsch.13314062.html`.
    # Search/category pages instead end in small dotted groups
    # (`.../wg-zimmer-in-Luzern.152.0.1.0.html`) — none of which is 6+ digits,
    # so they are rejected. Matches WG-room and flat detail pages alike.
    listing_url_pattern = re.compile(
        r"wg-gesucht\.de/[^\s\"'<>]*\.\d{6,}\.html",
        re.IGNORECASE,
    )
