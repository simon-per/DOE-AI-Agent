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

# Canonical HR / recruiter title detector. Single source of truth — also imported
# by discover_contacts.py to gate Source 3 (Google) hits. Word-boundary regex
# avoids false positives ("Personal Assistant", "people analytics") while still
# catching short headlines ("HR", "HRBP", "Head of HR") that a substring like
# "hr " (trailing space) misses.
_HR_TITLE_RE = re.compile(
    r"\b(?:"
    r"recruit\w*|"             # Recruiter, Recruiting, Recruitment
    r"talent\s*acquisition|"   # Talent Acquisition (avoids matching "talent" alone in random titles)
    r"talent\s*partner|"
    r"hr|hrbp|"                # HR, HRBP, Head of HR (word-boundary anchored)
    r"human\s*resources|"
    r"personalwesen|personalleiter|personalreferent|personalmanagement|personalabteilung|"
    r"people\s*&?\s*culture|"  # People & Culture, People Culture
    r"head\s*of\s*people|"
    r"hiring\s*manager|hiring\s*partner"
    r")\b",
    re.IGNORECASE,
)


def is_recruiter_title(title: str | None) -> bool:
    """Return True if `title` looks like an HR / recruiting role.

    Used to gate writes from low-trust sources (Google search hits) and to
    classify confidence in posting extraction. Word-boundary anchored —
    "Personal Assistant" → False, "HR Business Partner" → True.
    """
    if not title:
        return False
    return bool(_HR_TITLE_RE.search(title))

# Email regex — same as web_search.py:405 for consistency
_EMAIL_RE = re.compile(r"\b([\w.+-]+@[\w.-]+\.\w{2,})\b")
# Generic mailboxes we want to skip (no individual recipient signal)
_GENERIC_MAILBOX_PREFIXES = (
    "info@", "hello@", "contact@", "kontakt@", "office@", "support@",
    "noreply@", "no-reply@", "donotreply@", "newsletter@", "marketing@",
    "press@", "media@", "sales@", "admin@",
)

# ---------------------------------------------------------------------------
# Name/email construction helpers (Source 3 pattern-anchored discovery)
# ---------------------------------------------------------------------------
# Pure functions used to: transliterate non-ASCII names for email construction,
# split a free-form display name into (first, last), infer the email pattern
# from a known email at a domain, and construct an email from a name+pattern.

import unicodedata as _unicodedata

# Honorifics/titles to strip when isolating first/last from a display name.
_HONORIFICS = (
    "frau", "herr", "fr.", "hr.", "ms", "mr", "mrs", "miss", "ms.", "mr.", "mrs.",
    "dr", "dr.", "prof", "prof.", "dipl", "dipl.", "ing", "ing.",
    "sig", "sig.", "sig.ra", "sigra", "sra", "dott", "dott.", "dottssa", "dott.ssa",
    "mme", "mlle", "m.",
)

# German-language digraph map applied BEFORE generic Unicode decomposition.
# Generic NFKD would turn ü→u (loses the umlaut), but Swiss/German email
# conventions transliterate ü→ue.
_GERMAN_DIGRAPHS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})


def _transliterate_to_ascii(s: str) -> str:
    """Lowercase + transliterate a name to ASCII for email construction.

    Pipeline: German digraphs (ä→ae, ö→oe, ü→ue, ß→ss) FIRST, then NFKD
    Unicode decomposition + ASCII strip for residual accents (é→e, à→a, ç→c).
    Preserves hyphens. Drops apostrophes (O'Brien → obrien).

    Examples: Müller→mueller, Schlüsselbänder→schluesselbaender, Renée→renee,
    Côté→cote, Straße→strasse, O'Brien→obrien, Müller-Schmidt→mueller-schmidt.
    """
    if not s:
        return ""
    s = s.translate(_GERMAN_DIGRAPHS)
    decomposed = _unicodedata.normalize("NFKD", s)
    ascii_only = "".join(c for c in decomposed if not _unicodedata.combining(c))
    ascii_only = ascii_only.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.lower()
    # Keep letters, digits, and hyphens; drop everything else (apostrophes, dots, spaces stay for split-handling below).
    out = []
    for ch in ascii_only:
        if ch.isalnum() or ch in ("-", " "):
            out.append(ch)
    return "".join(out)


def _split_name_for_email(name: str) -> tuple[str, str] | None:
    """Split a display name into (first_ascii, last_ascii) for email construction.

    Strips honorifics, transliterates, takes the FIRST word as first name and
    the LAST word as surname (preserves hyphenated surnames as a single token).
    Returns None for single-word names (too ambiguous) or empty input.

    Examples:
      "Stefan Meier"             → ("stefan", "meier")
      "Frau Anna Müller"         → ("anna", "mueller")
      "Dr. Renée Côté"           → ("renee", "cote")
      "Anna Maria Müller-Schmidt" → ("anna", "mueller-schmidt")
      "Müller"                   → None  (single token; can't infer first)
    """
    if not name or not name.strip():
        return None
    # Tokenize on whitespace; transliterate each token; drop empties.
    raw_tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    tokens = [_transliterate_to_ascii(t).strip().strip("-") for t in raw_tokens]
    tokens = [t for t in tokens if t and t.lower() not in _HONORIFICS]
    if len(tokens) < 2:
        return None
    first = tokens[0]
    last = tokens[-1]
    if not first or not last:
        return None
    return (first, last)


# Patterns we try to match. Each pattern is identified by a format string that
# uses {first}, {last}, {f} (first initial), {l} (last initial). The order of
# this list is the preference order when ambiguous.
_EMAIL_PATTERN_FORMATS = (
    "{first}.{last}",
    "{last}.{first}",
    "{first}_{last}",
    "{f}{last}",
    "{first}{l}",
    "{first}{last}",
    "{first}",
)


def _infer_email_pattern(known_email: str, domain: str) -> str | None:
    """Given a known person-specific email at `domain`, infer the format string.

    Returns one of `_EMAIL_PATTERN_FORMATS` or None when the local part is too
    ambiguous to pattern-match (e.g., single-token "amueller" could be `{f}{last}`
    for any number of names — not safe to invert without a second example).

    The function is INFORMATIONAL — it does not validate the email domain
    matches `domain`. Caller is responsible for that filter.
    """
    if not known_email or "@" not in known_email:
        return None
    local = known_email.split("@", 1)[0].lower()
    # Split on common separators
    tokens = re.split(r"[._-]+", local)
    tokens = [t for t in tokens if t]
    if len(tokens) == 2:
        a, b = tokens
        # 2 multi-letter tokens with dot ⇒ first.last or last.first.
        # We can't tell which without ground truth; default to first.last
        # (the dominant Western convention).
        sep = "_" if "_" in local else ("-" if "-" in local else ".")
        if sep == ".":
            return "{first}.{last}"
        if sep == "_":
            return "{first}_{last}"
        return "{first}.{last}"  # hyphen rare; treat as dot
    if len(tokens) == 1:
        only = tokens[0]
        # Single-token: heuristics. Pure-alpha length-2-4 ⇒ probably initials,
        # too ambiguous. Length 5+ with no separator ⇒ could be {f}{last}
        # (most common single-token corporate pattern) — accept it.
        if len(only) < 5:
            return None
        # Default single-token assumption is {f}{last}.
        return "{f}{last}"
    return None


def _construct_email(first: str, last: str, pattern: str, domain: str) -> str | None:
    """Apply a format string from `_EMAIL_PATTERN_FORMATS` to produce an email.

    Returns None on missing inputs or unknown format placeholders.
    """
    if not first or not last or not pattern or not domain:
        return None
    if pattern not in _EMAIL_PATTERN_FORMATS:
        return None
    try:
        local = pattern.format(
            first=first, last=last,
            f=first[0] if first else "",
            l=last[0] if last else "",
        )
    except (KeyError, IndexError):
        return None
    if not local or "@" in local:
        return None
    return f"{local}@{domain.lower()}"


# ---------------------------------------------------------------------------
# End name/email construction helpers
# ---------------------------------------------------------------------------

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

    # Step 2: LLM fallback (cheap, ~150 tokens). Only when keys provided AND we're missing
    # at least one field that the LLM might find. Skip the LLM only when the
    # regex already produced name+email+honorific (the cheap-path early exit).
    needs_llm = not (out["contact_name"] and out["contact_email"] and out["contact_honorific"])

    if needs_llm and (openrouter_key or gemini_key):
        llm_data = _llm_extract_contact_dict(description, openrouter_key, gemini_key)
        # Only fill blanks — never overwrite a regex hit
        for key in ("contact_name", "contact_email", "contact_title", "contact_honorific"):
            if not out[key] and llm_data.get(key):
                out[key] = llm_data[key]
        if llm_data.get("source_quote"):
            out["source_quote"] = llm_data["source_quote"]

    # Confidence rubric (uniform across regex-only and LLM-fallback paths):
    # "high" requires name + email + honorific + an explicit HR/recruiter
    # title. Everything else falls to medium / low.
    has_hr_title = is_recruiter_title(out.get("contact_title"))
    if out["contact_name"] and out["contact_email"] and out["contact_honorific"] and has_hr_title:
        out["confidence"] = "high"
    elif out["contact_name"] and out["contact_email"]:
        out["confidence"] = "medium"
    elif out["contact_name"] or out["contact_email"]:
        out["confidence"] = "low"
    else:
        out["confidence"] = "low"

    return out


_LLM_TEXT_MAX_CHARS = 12_000  # upper bound for LLM input — fits 5k JDs (Source 1) and downsampled 24k pages (Source 2)


# Unicode look-alikes the LLM tends to substitute when "quoting" verbatim.
# Maps smart quotes, en/em dashes, and the German "ß" so that quote-vs-text
# comparison survives stylistic LLM rewrites of typographic characters.
_QUOTE_NORMALIZE_TABLE = str.maketrans({
    "‘": "'",  # left single quote
    "’": "'",  # right single quote / apostrophe
    "‚": "'",  # single low-9 quote
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "„": '"',  # double low-9 quote
    "–": "-",  # en dash
    "—": "-",  # em dash
    "−": "-",  # minus sign
    "…": "...",  # ellipsis
    " ": " ",  # NBSP (defensive; \s+ already covers it under re.UNICODE)
    " ": " ",  # narrow NBSP
})


def _quote_in_text(quote: str, text: str) -> bool:
    """True if `quote` appears in `text` after normalization.

    Defensive substring check used to verify the LLM's `source_quote` field is
    grounded in actual text we showed it. Catches the rare case where the model
    invents a plausible-looking sentence that isn't actually in the posting.

    Normalization (applied to both sides):
      1. NFKC compatibility normalization (folds ligatures, full-width forms,
         etc. to their ASCII equivalents where applicable).
      2. Smart quotes / en/em dashes / ellipsis → ASCII (LLMs commonly
         substitute these when quoting).
      3. Lowercase.
      4. Collapse all whitespace runs (incl. newlines, tabs, NBSP) to single
         spaces; strip.

    Stricter than a fuzzy match — the LLM was instructed to copy verbatim, so
    verbatim is what we verify. But typographic differences alone won't cause
    a false reject.
    """
    if not quote or not text:
        return False
    import unicodedata

    def _norm(s: str) -> str:
        normalized = unicodedata.normalize("NFKC", s).translate(_QUOTE_NORMALIZE_TABLE)
        return re.sub(r"\s+", " ", normalized.lower()).strip()

    nq, nt = _norm(quote), _norm(text)
    if not nq:
        return False
    return nq in nt


def _build_extraction_excerpt(text: str) -> str:
    """Compose the excerpt the LLM sees from `text`.

    Short inputs pass through verbatim. Long inputs (Source 2 re-fetched listing
    pages can be up to 24k chars) are downsampled to a head+tail composition so
    BOTH the page header (sometimes has sidebar contact widget) AND the page
    footer (where "Ihre Ansprechpartnerin …" lives in most DE/CH postings) make
    it into the LLM call. The middle section — usually benefits, diversity
    statement, generic role description — is the part safe to drop.
    """
    if not text:
        return ""
    if len(text) <= _LLM_TEXT_MAX_CHARS:
        return text
    separator = "\n\n[... mid-page content elided ...]\n\n"
    head_budget = 7_000
    tail_budget = _LLM_TEXT_MAX_CHARS - head_budget - len(separator)
    return text[:head_budget] + separator + text[-tail_budget:]


def _llm_extract_contact_dict(
    description: str,
    openrouter_key: str | None,
    gemini_key: str | None,
) -> dict:
    """LLM call returning a strict JSON dict. Errors → empty dict (caller handles).

    Feeds the LLM the full posting when short enough; head+tail composition
    when oversized (see `_build_extraction_excerpt`). The previous 1500-char
    tail dropped contact widgets and headers in long postings.
    """
    body_excerpt = _build_extraction_excerpt(description)

    prompt = (
        "Extract the APPLICATION CONTACT from THIS job posting excerpt. "
        "Return ONLY valid JSON matching this exact schema:\n"
        '{"contact_name": "...", "contact_email": "...", "contact_title": "...", '
        '"contact_honorific": "...", "source_quote": "..."}\n\n'
        "STRICT rules — return null for ALL fields unless you find one of these "
        "explicit signals:\n"
        "  a) A dedicated contact block (\"Ihre Ansprechpartnerin\", \"Ihr "
        "Ansprechpartner\", \"Kontakt\", \"Bei Fragen wenden Sie sich an …\", "
        "\"Your contact\", \"Persona di contatto\") naming a specific person, OR\n"
        "  b) A signature / sign-off line under the posting body with a name + "
        "honorific (\"Frau X\", \"Herr Y\", \"Ms X\", \"Mr Y\"), OR\n"
        "  c) An email address shown next to a name whose title contains an HR / "
        "recruiting role marker (Recruiter, Recruiting, Talent Acquisition, HR, "
        "Human Resources, Personalwesen, Personalleiter, People & Culture).\n\n"
        "Do NOT extract:\n"
        "- A CEO / founder / managing director mentioned in the company description.\n"
        "- A hiring manager named only as the role's reporting line (\"You will "
        "report to …\").\n"
        "- An author byline from a job-board template.\n"
        "- A team-member name listed only for context.\n\n"
        "Field rules:\n"
        "- contact_name: full name of the explicit application contact, or null.\n"
        "- contact_email: their direct email if explicitly attached to that person, "
        "or null. Generic mailboxes (info@, hr@, jobs@, kontakt@) are acceptable "
        "ONLY when they appear inside a dedicated contact block (signal a).\n"
        "- contact_title: their role/title (e.g. 'HR Business Partner', "
        "'Recruiter'), or null.\n"
        "- contact_honorific: 'Frau' or 'Herr' or 'Ms' or 'Mr' if explicitly "
        "stated; else null.\n"
        "- source_quote: the exact sentence from the text where you found the "
        "signal, or null. REQUIRED when any other field is non-null — if you "
        "cannot quote the source, return null for everything.\n\n"
        f"Text:\n{body_excerpt}"
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
            log.info(f"  LLM/{provider} extraction: email failed regex, dropped: {email!r}")
            cleaned["contact_email"] = None

        # Strict rule: if the LLM returned a name/email/title but couldn't quote
        # the source, treat the whole extraction as a hallucination and drop it.
        any_signal = any(cleaned.get(k) for k in ("contact_name", "contact_email", "contact_title"))
        if any_signal and not cleaned.get("source_quote"):
            log.info(f"  LLM/{provider} extraction rejected (no source_quote — likely hallucination)")
            return {}

        # Tighter rule: the source_quote must actually appear in the text we
        # showed the model. Catches invented quotes that pass the first guard
        # by being plausible-looking but not grounded in the posting.
        if any_signal and cleaned.get("source_quote"):
            if not _quote_in_text(cleaned["source_quote"], body_excerpt):
                log.info(
                    f"  LLM/{provider} extraction rejected "
                    f"(source_quote not in text — likely hallucination): "
                    f"{cleaned['source_quote'][:80]!r}"
                )
                return {}

        if cleaned.get("contact_name") or cleaned.get("contact_email"):
            log.info(f"  Contact info found (LLM/{provider}): "
                     f"name={cleaned.get('contact_name')}, email={cleaned.get('contact_email')}")
        return cleaned
    except Exception as e:
        log.debug(f"  LLM contact dict extraction failed: {e}")
        return {}
