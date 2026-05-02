"""
Contact person extraction — consolidated from generate_cover_letter.py and write_jobs_to_sheet.py.

Provides regex-based extraction (free, fast) and optional LLM fallback (cheap, ~100 tokens).
Supports German, Swiss German, English, and Italian job postings.

Usage:
    from execution.extract_contacts import extract_contact_person, extract_contacts_from_posting

    name = extract_contact_person(description)              # regex only (for sheet)
    name = extract_contact_person(description, openrouter_key=key)  # with LLM fallback

    # New richer extractor used by discover_contacts.py — returns name + email + title:
    info = extract_contacts_from_posting(description, openrouter_key=key, gemini_key=key)
    # → {"contact_name": "Anna Müller", "contact_email": "...", "contact_title": "...",
    #    "contact_honorific": "Frau", "source_quote": "...", "confidence": "high"|"medium"|"low"}
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Email regex — same as web_search.py:405 for consistency
_EMAIL_RE = re.compile(r"\b([\w.+-]+@[\w.-]+\.\w{2,})\b")
# Generic mailboxes we want to skip (no individual recipient signal)
_GENERIC_MAILBOX_PREFIXES = (
    "info@", "hello@", "contact@", "kontakt@", "office@", "support@",
    "noreply@", "no-reply@", "donotreply@", "newsletter@", "marketing@",
    "press@", "media@", "sales@", "admin@",
)

# Name pattern: 2-3 capitalized words (handles umlauts and accents)
_NAME = r"([A-ZÄÖÜÀÈÉÌÒÙ][a-zäöüßàèéìòù]+(?:\s+[A-ZÄÖÜÀÈÉÌÒÙ][a-zäöüßàèéìòù]+){1,2})"

_CONTACT_PATTERNS = [
    # German: "Ansprechperson / Ansprechpartner(in) / Kontaktperson / Ihr(e) Ansprechpartner(in): [Name]"
    rf"(?:Ansprechperson|Ansprechpartner(?:in)?|Kontaktperson|Ihr(?:e)?\s+Ansprechpartner(?:in)?)\s*[:\-–]?\s*(?:Frau|Herr)?\s*{_NAME}",
    # German: "Bei Fragen ... wenden Sie sich an [Name]" / "Fragen beantwortet"
    rf"(?:Bei\s+Fragen\b[^.]{{0,40}}wenden\s+Sie\s+sich\s+an|Fragen\s+beantwortet)\s*[:\-–]?\s*(?:Frau|Herr)?\s*{_NAME}",
    # Swiss German: "Bei Fragen steht dir/Ihnen [Name] ... zur Verfügung"
    rf"(?:Bei\s+Fragen\s+steht\s+(?:dir|Ihnen))\s+{_NAME}",
    # Swiss German: "[Name], [Title], steht dir/Ihnen bei Fragen"
    rf"{_NAME},\s+[A-ZÄÖÜ][^,]{{5,40}},\s+steht\s+(?:dir|Ihnen)\s+(?:bei\s+Fragen|gerne)",
    # German: "Kontakt: [Name]" (simple)
    rf"(?:Kontakt)\s*[:\-–]\s*(?:Frau|Herr)?\s*{_NAME}",
    # English
    rf"(?:Contact\s*(?:person)?|Recruiter|Hiring\s+Manager|Your\s+contact)\s*[:\-–]?\s*(?:Ms\.?|Mr\.?|Mrs\.?)?\s*{_NAME}",
    # Italian
    rf"(?:Persona\s+di\s+contatto|Contatto|Referente)\s*[:\-–]?\s*(?:Sig\.?(?:ra)?|Dott\.?(?:ssa)?)\s*{_NAME}",
]


def extract_contact_person(
    description: str,
    openrouter_key: str | None = None,
    gemini_key: str | None = None,
) -> str | None:
    """Extract a contact person name from a job description.

    Tries regex patterns first (free), then optional LLM fallback (cheap, ~100 tokens).
    Returns the name if found with reasonable confidence, else None.
    """
    if not description or description.strip() in ("", "nan"):
        return None

    # Regex extraction (free, fast)
    name = _extract_contact_regex(description)
    if name:
        return name

    # LLM fallback (only if keys provided)
    if openrouter_key or gemini_key:
        return _extract_contact_llm(description, openrouter_key, gemini_key)

    return None


def _extract_contact_regex(description: str) -> str | None:
    """Extract contact person using regex patterns."""
    for pattern in _CONTACT_PATTERNS:
        match = re.search(pattern, description)
        if match:
            name = match.group(1).strip()
            parts = name.split()
            if 2 <= len(parts) <= 4 and len(name) < 60:
                log.info(f"  Contact person found (regex): {name}")
                return name
    return None


def _extract_contact_llm(
    description: str,
    openrouter_key: str | None,
    gemini_key: str | None,
) -> str | None:
    """Use LLM to extract contact person name when regex fails. Very cheap (~100 tokens)."""
    # Only search the last 800 chars where contact info typically appears
    tail = description[-800:] if len(description) > 800 else description

    prompt = (
        "Extract the contact person's full name from this job posting excerpt. "
        "Return ONLY the name (e.g. 'Marc Zeugin'), or 'NONE' if no contact person is mentioned.\n\n"
        f"Text:\n{tail}"
    )

    try:
        from execution.llm_client import call_llm
        result, provider = call_llm(openrouter_key, gemini_key, prompt, max_tokens=100)
        result = result.strip().strip('"').strip("'")
        if result and result.upper() != "NONE" and 2 <= len(result.split()) <= 3 and len(result) < 40:
            words = result.split()
            if all(w[0].isupper() for w in words if w):
                log.info(f"  Contact person found (LLM/{provider}): {result}")
                return result
    except Exception as e:
        log.debug(f"  LLM contact extraction failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Richer extractor — returns name + email + title + honorific in one call.
# Used by discover_contacts.py (Source 1 of the contact-discovery waterfall).
# ---------------------------------------------------------------------------

# Honorific markers that indicate gender for downstream salutation
_HONORIFIC_RE = re.compile(
    r"\b(Frau|Herr|Sehr\s+geehrte\s+Frau|Sehr\s+geehrter\s+Herr|"
    r"Ms\.?|Mr\.?|Mrs\.?|"
    r"Sig\.?(?:ra)?|Dott\.?(?:ssa)?)\s+([A-ZÄÖÜÀÈÉÌÒÙ][\w\s\-äöüßàèéìòù]{2,40})",
    re.IGNORECASE,
)


def _detect_honorific(text: str) -> str | None:
    """Return canonical honorific ('Frau' | 'Herr' | 'Ms' | 'Mr') from text, or None."""
    if not text:
        return None
    m = _HONORIFIC_RE.search(text)
    if not m:
        return None
    raw = m.group(1).lower()
    if "frau" in raw or raw.startswith("ms") or raw.startswith("mrs") or "sig.ra" in raw or "dott.ssa" in raw:
        return "Frau"
    if "herr" in raw or raw.startswith("mr") or raw.startswith("sig.") or raw.startswith("dott."):
        return "Herr"
    return None


# Public alias so generate_cover_letter.py can use the helper without dunder access
detect_honorific = _detect_honorific


def _extract_email_from_text(description: str) -> str | None:
    """Find the most-individual-looking email in `description`.

    Skips generic mailboxes (info@, hr@, etc.) — we want a person's address.
    Returns the first match satisfying the filter, or None.
    """
    candidates = _EMAIL_RE.findall(description or "")
    if not candidates:
        return None
    # Prefer non-generic mailboxes
    for email in candidates:
        lower = email.lower()
        if not any(lower.startswith(prefix) for prefix in _GENERIC_MAILBOX_PREFIXES):
            return email
    # Fall through: even a generic mailbox is better than nothing
    return candidates[0]


def extract_contacts_from_posting(
    description: str,
    openrouter_key: str | None = None,
    gemini_key: str | None = None,
) -> dict:
    """Extract structured contact info (name, email, title, honorific) from a job posting.

    Strategy:
      1. Regex pass — cheap, deterministic. Picks up the obvious cases (Ansprechperson,
         Frau/Herr Müller, plus an email if present).
      2. Optional LLM pass — only when keys are provided AND the regex returned nothing
         useful. Uses strict JSON via `json_mode=True` and falls back gracefully on errors.

    Always returns a dict with the same shape, even on total failure (all-None values).
    Never raises — callers can rely on the schema.
    """
    out: dict = {
        "contact_name": None,
        "contact_email": None,
        "contact_title": None,
        "contact_honorific": None,
        "source_quote": None,
        "confidence": "low",
    }

    if not description or not description.strip() or description.strip() == "nan":
        return out

    # Step 1: regex-based extraction (free)
    name = _extract_contact_regex(description)
    email = _extract_email_from_text(description)
    honorific = _detect_honorific(description)

    if name:
        out["contact_name"] = name
    if email:
        out["contact_email"] = email
    if honorific:
        out["contact_honorific"] = honorific

    # If regex got both a name and email → high confidence, skip LLM
    if out["contact_name"] and out["contact_email"]:
        out["confidence"] = "high"
        return out

    # Step 2: LLM fallback (cheap, ~150 tokens). Only when keys provided AND we're missing
    # at least one field that the LLM might find.
    if not (openrouter_key or gemini_key):
        # Whatever the regex found is what we have
        if out["contact_name"] or out["contact_email"]:
            out["confidence"] = "medium"
        return out

    llm_data = _llm_extract_contact_dict(description, openrouter_key, gemini_key)
    # Only fill blanks — never overwrite a regex hit
    for key in ("contact_name", "contact_email", "contact_title", "contact_honorific"):
        if not out[key] and llm_data.get(key):
            out[key] = llm_data[key]
    if llm_data.get("source_quote"):
        out["source_quote"] = llm_data["source_quote"]

    # Confidence rubric
    if out["contact_name"] and out["contact_email"]:
        out["confidence"] = "high"
    elif out["contact_name"] or out["contact_email"]:
        out["confidence"] = "medium"
    else:
        out["confidence"] = "low"

    return out


def _llm_extract_contact_dict(
    description: str,
    openrouter_key: str | None,
    gemini_key: str | None,
) -> dict:
    """LLM call returning a strict JSON dict. Errors → empty dict (caller handles).

    Looks at the last ~1500 chars where contact info typically lives in postings.
    """
    tail = description[-1500:] if len(description) > 1500 else description

    prompt = (
        "Extract contact information from THIS job posting excerpt. "
        "Return ONLY valid JSON matching this exact schema:\n"
        '{"contact_name": "...", "contact_email": "...", "contact_title": "...", '
        '"contact_honorific": "...", "source_quote": "..."}\n\n'
        "Rules:\n"
        "- contact_name: full name of the recruiter / hiring manager / contact person, or null\n"
        "- contact_email: their direct email if explicitly stated, or null. Skip generic "
        "mailboxes like info@/hr@/jobs@ unless that is the only one given.\n"
        "- contact_title: their role/title (e.g. 'HR Business Partner', 'Recruiter'), or null\n"
        "- contact_honorific: 'Frau' or 'Herr' or 'Ms' or 'Mr' if explicitly stated; else null\n"
        "- source_quote: the exact sentence from the text where you found the info, or null\n"
        "- Do NOT guess. If a field is not explicitly in the text, return null for it.\n\n"
        f"Text:\n{tail}"
    )

    try:
        from execution.llm_client import call_llm, parse_json_response
        response, provider = call_llm(
            openrouter_key, gemini_key, prompt,
            temperature=0.1, max_tokens=300, json_mode=True,
        )
        data = parse_json_response(response) or {}
        # Sanity-coerce all fields to str | None
        cleaned = {}
        for key in ("contact_name", "contact_email", "contact_title", "contact_honorific", "source_quote"):
            val = data.get(key)
            if isinstance(val, str) and val.strip() and val.strip().lower() not in ("null", "none", "n/a"):
                cleaned[key] = val.strip()
            else:
                cleaned[key] = None

        # Validate the email if present (don't trust LLM blindly)
        email = cleaned.get("contact_email")
        if email and not _EMAIL_RE.fullmatch(email):
            cleaned["contact_email"] = None

        if cleaned.get("contact_name") or cleaned.get("contact_email"):
            log.info(f"  Contact info found (LLM/{provider}): "
                     f"name={cleaned.get('contact_name')}, email={cleaned.get('contact_email')}")
        return cleaned
    except Exception as e:
        log.debug(f"  LLM contact dict extraction failed: {e}")
        return {}
