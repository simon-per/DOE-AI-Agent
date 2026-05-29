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
        out: list[ParsedListing] = []
        for url in urls:
            window = context_window(combined, url)
            subject = (msg.get("Subject") or "").strip()
            raw_text = "\n".join(part for part in (subject, window) if part)
            out.append(
                ParsedListing(
                    url=url,
                    source=self.source,
                    title=subject or None,
                    rent_chf=None,
                    city=None,
                    move_in=None,
                    raw_text=raw_text,
                )
            )
        return out
