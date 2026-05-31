"""Parser for UrbanHome.ch saved-search ("Suchabo") alert emails.

UrbanHome is a Swiss apartment/WG aggregator with a daily email alert and some
exclusive direct listings. Overlap with other sources collapses via the
apartment_pipeline canonical/content-key dedup on upsert.

NOTE: UrbanHome renders listing links client-side, so the exact detail-URL path
could not be confirmed remotely. `listing_url_pattern` below is a deliberately
conservative heuristic (a urbanhome URL outside `/suchen/` carrying a long
numeric id) that errs toward *under*-matching — a miss yields zero rows, never
junk. Finalize it from the first real alert: an odd sender dumps to
`.tmp/email_samples/`; read the `.eml`, tighten the pattern, rerun with
`--reprocess`. See directives/ingest_email_alerts.md.
"""

from __future__ import annotations

import re

from .base import BaseEmailParser


class UrbanHomeParser(BaseEmailParser):
    source = "urbanhome.ch"
    sender_patterns = (
        re.compile(r"[@.]urbanhome\.(?:ch|net)\b", re.IGNORECASE),
    )
    imap_search_domains = ("urbanhome.ch", "urbanhome.net")
    # Provisional: a detail URL is a urbanhome link whose PATH ends in a numeric
    # listing-id segment (`/<6+ digits>`) and is not under a (optionally
    # locale-prefixed) `/suchen/` search path. Anchoring the id to the path --
    # not the query string -- stops category/footer links that carry an
    # incidental long number (`?mc_eid=...123456`, `?page=100000`) from being
    # mistaken for listings, so a wrong guess yields zero rows, never junk.
    # Category pages (/suchen/..., /wohnung-mieten-luzern, /luzern, /Aargau/)
    # have no such path id and are rejected.
    listing_url_pattern = re.compile(
        r"urbanhome\.(?:ch|net)/(?!(?:[a-z]{2}/)?suchen/)[^\s\"'<>?]*/\d{6,}(?:[/?#]|$)",
        re.IGNORECASE,
    )
