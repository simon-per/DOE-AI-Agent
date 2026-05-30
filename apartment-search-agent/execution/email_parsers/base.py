"""Base class + helpers for portal email-alert parsers.

A parser converts one alert email into zero-or-more `ParsedListing`s. We do
NOT scrape the portal — the email body itself is the source of truth. The
existing apartment_pipeline normalize/score layer will fill in rent/city/etc.
from the surrounding text and URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message
from html import unescape
from typing import Iterable

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t ]+")


# Currency-tagged amount. Two arms: "CHF 1'250.00" (currency first) and
# "1'250.- CHF" (amount first). The amount-first arm carries negative
# lookbehinds so a grouped phone fragment ("079 123 4567 CHF") or a longer
# digit run can't be misread as rent. Decimal/grouping normalization happens
# in _normalize_rent_token, not in this pattern.
_RENT_RE = re.compile(
    r"(?:CHF|Fr\.?)\s*(?P<pre>\d[\d'’.,]*\d|\d)"
    r"|(?<!\d\s)(?<![\d.,'’])(?P<post>\d[\d'’.,]*\d|\d)(?:[.,][-–—])?\s*(?:CHF|Fr\.?)\b",
    re.IGNORECASE,
)

# Peels a trailing cents group (".00" / ",50") so the integer part can have
# its thousands separators stripped cleanly afterwards.
_RENT_CENTS_RE = re.compile(r"^(?P<intpart>.*?)(?:[.,](?P<cents>\d{1,2}))?$")
_RENT_SEP_CHARS = ".,'’ "


class ParseError(Exception):
    """Raised when an email matches a parser's sender but cannot be parsed."""


@dataclass(frozen=True)
class ParsedListing:
    url: str
    source: str
    title: str | None
    rent_chf: int | None
    city: str | None
    move_in: str | None
    raw_text: str


def html_to_text(html: str) -> str:
    """Best-effort plaintext extraction from a portal alert HTML body."""
    if not html:
        return ""
    # Drop scripts/styles wholesale before tag stripping.
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return "\n".join(line.strip() for line in cleaned.splitlines() if line.strip())


def extract_message_body(msg: Message) -> tuple[str, str]:
    """Return (plain_text, html) bodies. Either may be empty."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        content_type = (part.get_content_type() or "").lower()
        if content_type not in ("text/plain", "text/html"):
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, AttributeError):
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)
    return "\n".join(plain_parts), "\n".join(html_parts)


def context_window(body_text: str, url: str, radius: int = 240) -> str:
    """Return text around `url` to give the pipeline rent/city/title cues."""
    idx = body_text.find(url)
    if idx < 0:
        return body_text[:radius]
    start = max(0, idx - radius)
    end = min(len(body_text), idx + len(url) + radius)
    return body_text[start:end]


def dedupe_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        # Strip trailing punctuation often included by HTML-to-text conversion.
        cleaned = url.rstrip(").,;:!?’”>")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def extract_anchor_text(html: str, url: str) -> str | None:
    """Return the visible text inside <a href="URL...">...</a> if present.

    Portal alerts usually wrap each listing URL in an anchor whose visible
    text is the listing's headline (city, room count, rent). That's a much
    better `title` than the digest-style subject we'd otherwise fall back to.

    Matches on a URL *prefix* so an href that quotes the listing URL and then
    appends tracking params (`?utm_source=alert`) still resolves, and tolerates
    unquoted hrefs (`href=https://...`).
    """
    if not html or not url:
        return None
    pattern = (
        r"<a\b[^>]*\bhref\s*=\s*[\"']?"
        + re.escape(url)
        + r"[^\s>\"']*[\"']?[^>]*>(?P<inner>.*?)</a>"
    )
    match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    inner = html_to_text(match.group("inner")).strip()
    return inner or None


def _normalize_rent_token(raw: str) -> int | None:
    """Turn a matched amount token (`1'250.00`, `850.-`, `1.250`) into an int.

    Strips a trailing cents/dash group first, then thousands separators.
    Rejects tokens carrying more than one separator in the integer part —
    those look like dates ("1.7.2026") or grouped phone numbers, not rent.
    """
    token = raw.strip().rstrip("-–—.")
    if not token:
        return None
    cents_match = _RENT_CENTS_RE.match(token)
    intpart = (
        cents_match.group("intpart")
        if cents_match and cents_match.group("intpart")
        else token
    )
    if sum(intpart.count(sep) for sep in _RENT_SEP_CHARS) > 1:
        return None
    digits = re.sub(r"[.,'’ ]", "", intpart)
    if not digits.isdigit():
        return None
    return int(digits)


def extract_rent_chf(text: str) -> int | None:
    """First plausible CHF-tagged amount in `text`, normalized to int.

    Returns None when nothing parseable shows up. Skips amounts under 100
    (almost never monthly rent) and over 10,000 (deposits, yearly figures).
    Amounts with explicit cents (`CHF 850.00`) and apostrophe grouping
    (`CHF 1'250`) normalize correctly; dates and phone fragments do not
    produce a false positive."""
    if not text:
        return None
    for match in _RENT_RE.finditer(text):
        raw = match.group("pre") or match.group("post") or ""
        value = _normalize_rent_token(raw)
        if value is not None and 100 <= value <= 10_000:
            return value
    return None


class BaseEmailParser:
    """Parsers extract listing URLs on the portal's own domain and a context
    window for each. Subclasses set portal-specific identifiers."""

    source: str = ""
    sender_patterns: tuple[re.Pattern, ...] = ()
    # Plain domain substrings used for IMAP FROM searches. IMAP does not
    # understand regex, so we keep this separate from `sender_patterns`.
    imap_search_domains: tuple[str, ...] = ()
    listing_url_pattern: re.Pattern = re.compile(r"^$")

    def matches_sender(self, from_header: str) -> bool:
        normalized = (from_header or "").lower()
        return any(p.search(normalized) for p in self.sender_patterns)

    def listing_urls(self, combined_text: str) -> list[str]:
        urls = (m.group(0) for m in URL_RE.finditer(combined_text))
        listing = (u for u in urls if self.listing_url_pattern.search(u))
        return dedupe_urls(listing)

    def enrich(
        self,
        url: str,
        window: str,
        html: str,
        anchor_text: str | None,
        subject: str,
    ) -> dict:
        """Per-portal hook to extract rent/city/title/move_in.

        Default behavior: anchor text → title (richer than subject), generic
        CHF regex → rent_chf. Subclasses override to apply portal-specific
        HTML structure when needed (e.g. tables in Homegate alerts).
        """
        return {
            "title": anchor_text or subject or None,
            "rent_chf": extract_rent_chf(window) or extract_rent_chf(subject),
            "city": None,
            "move_in": None,
        }

    def parse(self, msg: Message) -> list[ParsedListing]:
        plain, html = extract_message_body(msg)
        text_from_html = html_to_text(html)
        combined = "\n".join(part for part in (plain, text_from_html) if part)
        # Prefer extracting URLs from the raw HTML — anchor hrefs survive the
        # markup-strip; the plaintext alternative often lists URLs too.
        url_source = "\n".join(part for part in (html, plain) if part)
        urls = self.listing_urls(url_source)
        if not urls:
            return []
        subject = (msg.get("Subject") or "").strip()
        out: list[ParsedListing] = []
        for url in urls:
            window = context_window(combined, url)
            raw_text = "\n".join(part for part in (subject, window) if part)
            anchor_text = extract_anchor_text(html, url)
            fields = self.enrich(url, window, html, anchor_text, subject)
            out.append(
                ParsedListing(
                    url=url,
                    source=self.source,
                    title=fields.get("title") or subject or None,
                    rent_chf=fields.get("rent_chf"),
                    city=fields.get("city"),
                    move_in=fields.get("move_in"),
                    raw_text=raw_text,
                )
            )
        return out
