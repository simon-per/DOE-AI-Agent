"""Email pattern generator + lightweight SMTP RCPT verifier.

Used as Source 4 in the contact-discovery waterfall (`discover_contacts.py`).
Triggered only when sources 1–3 found a contact NAME but no email.

Strategy:
  1. Generate plausible email patterns from (first, last, domain).
  2. DNS MX lookup to confirm the domain accepts mail at all.
  3. Skip accept-all providers (Gmail, Outlook, *.protection.outlook.com) — they 250 OK
     for every address, so SMTP verify gives false positives. For these we return the
     most-common pattern with confidence='low' (an educated guess, not a verified hit).
  4. SMTP RCPT TO probe: connect, EHLO, MAIL FROM:<>, RCPT TO:<candidate>, QUIT.
     250 → email accepted. 550/551/553/554 → try next candidate. 5s socket timeout.

Hard caps: 4 patterns × 5s = 20s max per company. Errors swallowed → returns (None, "").

This module is deliberately stdlib-only except for `dnspython` (in requirements.txt).
No external network calls beyond DNS + outbound port 25 SMTP.

Usage:
    from execution.email_verifier import verify_pattern
    email, confidence = verify_pattern("Anna", "Müller", "example.ch")
    # → ("anna.mueller@example.ch", "low") or (None, "")
"""

from __future__ import annotations

import logging
import smtplib
import socket
import unicodedata
from typing import Iterable

log = logging.getLogger(__name__)

# Sender used in MAIL FROM: an empty <> "null sender" is the most polite probe (RFC 5321),
# but some servers reject it. Using a domain we control prevents bounce loops.
_PROBE_FROM = "preflight@example.com"

# Per-probe SMTP socket timeout. 5s is enough for a healthy server, short enough that
# 4 sequential probes take ≤20s.
_SMTP_TIMEOUT_SEC = 5.0

# Hosts whose MX records belong to providers that accept mail for *every* local-part
# (catch-all behavior) — verifying against them gives false positives. We skip the
# probe and return our best-guess pattern with confidence='low'.
_ACCEPT_ALL_MX_SUFFIXES = (
    "google.com.",
    "googlemail.com.",
    "gmail-smtp-in.l.google.com.",
    "outlook.com.",
    "protection.outlook.com.",
    "office365.com.",
    "mail.protection.outlook.com.",
)


def _strip_accents(s: str) -> str:
    """Normalize 'Müller' → 'Mueller', 'Étienne' → 'Etienne'.

    German umlauts get the conventional ae/oe/ue/ss expansion (closer to how addresses
    are typically created in DACH). Other diacritics get plain ASCII fallback.
    """
    # Pre-handle German umlauts before NFKD strips them
    replacements = {
        "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
        "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    }
    for src, dst in replacements.items():
        s = s.replace(src, dst)
    # NFKD removes remaining accents
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _normalize_part(s: str) -> str:
    """Lowercase + strip accents + keep [a-z0-9-] only."""
    s = _strip_accents(s).lower().strip()
    s = "".join(c if (c.isalnum() or c == "-") else "" for c in s)
    return s


def generate_patterns(first: str, last: str, domain: str) -> list[str]:
    """Generate candidate emails ordered by DACH frequency.

    Empty inputs → empty list. Caller decides whether to probe.
    """
    f = _normalize_part(first)
    l = _normalize_part(last)
    d = (domain or "").strip().lower().lstrip("@")
    if not (f and l and d):
        return []
    f_initial = f[0] if f else ""
    l_initial = l[0] if l else ""
    patterns_local = [
        f"{f}.{l}",
        f"{f_initial}.{l}",
        f"{f}",
        f"{f}{l}",
        f"{f}-{l}",
        f"{f_initial}{l}",
        f"{l}.{f}",
        f"{l}",
    ]
    # Dedup while preserving order
    seen = set()
    unique_locals: list[str] = []
    for p in patterns_local:
        if p and p not in seen:
            seen.add(p)
            unique_locals.append(p)
    return [f"{lp}@{d}" for lp in unique_locals]


def _resolve_mx(domain: str) -> list[str]:
    """Return MX hostnames for `domain`, lowest preference first. Empty list on DNS failure.

    Uses dnspython with explicit timeouts so a stuck DNS server can't hang the pipeline.

    A missing `dnspython` install is treated as a hard config error, not a soft DNS
    miss — otherwise every domain silently looks like "no MX records" and Source 4
    of the contact-discovery waterfall always returns empty without anyone noticing.
    """
    try:
        import dns.resolver
    except ImportError as exc:
        raise RuntimeError(
            "dnspython is not installed. Run `pip install -r requirements.txt` "
            "(dnspython is required for contact-email verification)."
        ) from exc

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = 3.0   # per-server timeout
        resolver.lifetime = 5.0  # total timeout including retries
        answers = resolver.resolve(domain, "MX")
        ranked = sorted(answers, key=lambda r: r.preference)
        return [str(r.exchange).lower().rstrip(".") + "." for r in ranked]
    except Exception as exc:  # noqa: BLE001 — actual DNS lookup fails in many ways, all benign here
        log.debug(f"  MX lookup failed for {domain}: {type(exc).__name__}: {exc}")
        return []


def _is_accept_all(mx_host: str) -> bool:
    """True if the MX host belongs to a known accept-all provider."""
    h = mx_host.lower().rstrip(".") + "."
    return any(h.endswith(suffix) for suffix in _ACCEPT_ALL_MX_SUFFIXES)


def _probe_smtp(mx_host: str, candidate: str, sender: str = _PROBE_FROM) -> bool | None:
    """Single SMTP RCPT TO probe.

    Returns:
        True   — server accepted (code 2xx)
        False  — server rejected with 5xx (mailbox does not exist / refused)
        None   — inconclusive (4xx temporary, network error, timeout)
    """
    try:
        with smtplib.SMTP(mx_host.rstrip("."), port=25, timeout=_SMTP_TIMEOUT_SEC) as smtp:
            smtp.ehlo_or_helo_if_needed()
            code, _ = smtp.mail(sender)
            if code >= 400:
                return None  # MAIL FROM rejected; can't conclude about the mailbox
            code, _ = smtp.rcpt(candidate)
            if 200 <= code < 300:
                return True
            if 500 <= code < 600:
                return False
            return None
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            smtplib.SMTPHeloError, smtplib.SMTPException,
            socket.timeout, socket.gaierror, OSError) as exc:
        log.debug(f"  SMTP probe to {mx_host} for {candidate}: {type(exc).__name__}: {exc}")
        return None


def verify_pattern(
    first: str,
    last: str,
    domain: str,
    candidates: Iterable[str] | None = None,
    max_attempts: int = 4,
) -> tuple[str | None, str]:
    """Return (best_candidate_email, confidence) for (first, last, domain).

    Confidence values:
        "low"  — a plausible address (verified by SMTP RCPT, OR an educated guess
                 against an accept-all provider). Always treat as best-effort.
        ""     — total miss; no domain MX, no probes succeeded.

    Never raises. Always finishes within ~20s worst case (4 patterns × 5s timeout).
    """
    patterns = list(candidates) if candidates is not None else generate_patterns(first, last, domain)
    if not patterns:
        return (None, "")

    mx_hosts = _resolve_mx((domain or "").strip().lower().lstrip("@"))
    if not mx_hosts:
        log.info(f"  No MX records for {domain} — skipping SMTP probe")
        return (None, "")

    primary_mx = mx_hosts[0]
    if _is_accept_all(primary_mx):
        # Accept-all hosts will say 250 OK to anything → can't verify. Best guess only.
        log.info(f"  {domain} uses accept-all MX ({primary_mx}); returning educated guess")
        return (patterns[0], "low")

    attempts = 0
    inconclusive: list[str] = []
    for candidate in patterns:
        if attempts >= max_attempts:
            break
        attempts += 1
        result = _probe_smtp(primary_mx, candidate)
        if result is True:
            log.info(f"  SMTP verified (250 OK): {candidate} via {primary_mx}")
            return (candidate, "low")
        if result is None:
            inconclusive.append(candidate)
        # False (5xx) → try next pattern silently

    # No definitive accept. If everything was inconclusive (e.g. greylist / port blocked),
    # return the most-common pattern with low confidence rather than nothing.
    if len(inconclusive) == attempts:
        log.info(f"  SMTP probes inconclusive for {domain}; returning most-common pattern as guess")
        return (patterns[0], "low")

    log.info(f"  SMTP probes rejected all {attempts} pattern(s) for {domain}")
    return (None, "")


def split_name(full_name: str) -> tuple[str, str]:
    """Split 'Anna Müller' → ('Anna', 'Müller'). Multi-word last names join with space.

    Returns ('', '') on empty input. Used by callers that have a single contact_name string.
    """
    parts = (full_name or "").strip().split()
    if len(parts) < 2:
        return ("", "")
    return (parts[0], " ".join(parts[1:]))
