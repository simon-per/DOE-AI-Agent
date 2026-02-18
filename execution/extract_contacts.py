"""
Contact person extraction — consolidated from generate_cover_letter.py and write_jobs_to_sheet.py.

Provides regex-based extraction (free, fast) and optional LLM fallback (cheap, ~100 tokens).
Supports German, Swiss German, English, and Italian job postings.

Usage:
    from execution.extract_contacts import extract_contact_person

    name = extract_contact_person(description)  # regex only (for sheet)
    name = extract_contact_person(description, openrouter_key=key)  # with LLM fallback
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

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
