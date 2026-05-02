"""
Shared utilities — consolidated from generate_cover_letter.py and generate_cv.py.

Usage:
    from execution.utils import sanitize_filename, strip_legal_suffixes, normalize_company_key, clean_job_title
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional


def generate_job_id(title: str, company: str, url: str) -> str:
    """Deterministic short Job ID: J-{first 6 hex of SHA256(title|company|url)}.

    Used as the primary key linking Google Sheet rows, application folders,
    and checkpoint files across all pipeline stages.
    """
    key = f"{title}|{company}|{url}"
    return f"J-{hashlib.sha256(key.encode()).hexdigest()[:6]}"


def sanitize_filename(name: str) -> str:
    """Sanitize a string for use as a filename. Max 80 chars."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:80]


# Legal entity suffixes common in Swiss/European company names
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(AG|GmbH|SA|Sàrl|Ltd\.?|Inc\.?|SE|KG|OHG|Co\.?|Gruppe|Group)\b",
    re.IGNORECASE,
)


def strip_legal_suffixes(company: str) -> str:
    """Strip legal entity suffixes (AG, GmbH, etc.) from a company name."""
    cleaned = _LEGAL_SUFFIX_RE.sub("", company).strip()
    cleaned = re.sub(r"\([^)]*\)", "", cleaned).strip()
    return cleaned.rstrip(",").strip()


def normalize_company_key(company: str) -> str:
    """Normalize company name for cache key / dedup matching (strip suffixes, lowercase)."""
    clean = strip_legal_suffixes(company)
    return clean.lower().strip() if clean else company.strip().lower()


def clean_job_title(title: str) -> str:
    """Strip URLs, percentage ranges, pipe-separated suffixes, and gender markers
    from job titles.

    Useful for folder names, subject lines, and display contexts where
    job-board noise should be removed. Reuses the gender-paren / bare-marker
    regexes defined below for clean_subtitle so the two cleaners stay in sync.
    """
    # Remove URLs (www.xxx.ch, https://...)
    title = re.sub(r'https?://\S+|www\.\S+', '', title)
    # Remove gender markers in parens: (m/w/d), (f/m/d), (all genders), (a), etc.
    title = _SUB_GENDER_PAREN.sub('', title)
    # Remove bare gender markers in compound titles: ", m/w/d" / "; f/m/d"
    title = _SUB_GENDER_IN_COMPOUND.sub('', title)
    # Remove standalone bare gender markers: " m/w/d"
    title = _SUB_GENDER_BARE.sub('', title)
    # Remove gendered word suffixes like "Manager:in" / "Berater/in" / "Manager*in"
    title = _SUB_GENDER_SUFFIX.sub('', title)
    # Remove percentage ranges like "80-100%" or "80–100%"
    title = re.sub(r'\d{2,3}\s*[\u2013-]\s*\d{2,3}\s*%', '', title)
    # Remove standalone percentages like "100%"
    title = re.sub(r'\b\d{2,3}\s*%', '', title)
    # Remove pipe-separated suffixes (e.g., "| Meilen (ZH) | www.qsome.ch")
    title = re.sub(r'\s*\|.*$', '', title)
    # Remove empty parentheses left over after stripping content (e.g. "(80-100%)" → "()")
    title = re.sub(r'\(\s*\)', '', title)
    # Collapse "  ," → "," (gender strip can leave "Manager , Sales" → "Manager, Sales")
    title = re.sub(r'\s+,', ',', title)
    # Clean up leftover whitespace and trailing punctuation
    return re.sub(r'\s+', ' ', title).strip().rstrip(' |,;\u2013-')


# --- clean_subtitle: deterministic cleaner for CL/CV header subtitle ---
# Produces a receiver-friendly subtitle from the raw job title by stripping
# aggregator/noise patterns (gender markers, percentages, contract types,
# ref codes) while preserving legitimate role context (product names,
# hyphenated specializations, cities).
# Rules validated against 140 random Google Sheet titles.

_SUB_ZERO_WIDTH = re.compile(r"[\u200B\u200C\u200D\uFEFF]")

_SUB_GENDER_PAREN = re.compile(
    r"\s*\(\s*(?:"
    r"[mwdfhxi](?:\s*[/,]\s*[mwdfhxi]){1,3}"    # (m/w/d), (f,m,d), (H/F), (f/m/i), (m/f/x/d)
    r"|all\s+genders?"
    r"|a"                                         # (a) — Swiss gender-neutral
    r")\s*\)",
    re.IGNORECASE,
)
_SUB_GENDER_BARE = re.compile(
    r"\s*\b[mwdfhxi](?:\s*/\s*[mwdfhxi]){1,3}\b",
    re.IGNORECASE,
)
_SUB_GENDER_IN_COMPOUND = re.compile(
    r"\s*[;,]\s*[mwdfhxi](?:\s*/\s*[mwdfhxi]){1,3}\b",
    re.IGNORECASE,
)
_SUB_GENDER_SUFFIX = re.compile(r"(?<=\w)(?::in|/-?in|/in|\*in)\b", re.IGNORECASE)

_SUB_PERCENT_PAREN_RANGE = re.compile(r"\s*\(\s*\d+\s*[-–]\s*\d+\s*%?\s*\)")
_SUB_PERCENT_PAREN_SINGLE = re.compile(r"\s*\(\s*\d+\s*%\s*\)")
_SUB_PERCENT_RANGE_PCT = re.compile(r"\s*,?\s*\d+\s*%\s*[-–]\s*\d+\s*%")
_SUB_PERCENT_RANGE_BARE = re.compile(r"\s*,?\s*[-–]?\s*\d+\s*[-–]\s*\d+\s*%")
_SUB_PERCENT_ANYWHERE = re.compile(r"\s*\d+\s*%")
_SUB_PENSUM = re.compile(r"\s*,?\s*Pensum\s+\d+\s*(?:[-–]\s*\d+)?\s*%?", re.IGNORECASE)

_SUB_CONTRACT_SPLIT = re.compile(
    r"\s+(?:Festanstellung|Teilzeit|Vollzeit|Tempor[aä]r|Befristet|Unbefristet|"
    r"Permanent|Full[-\s]?time|Part[-\s]?time|Fixed[-\s]?Term(?:\s+Contract)?|"
    r"Contract)\b.*$",
    re.IGNORECASE,
)

_SUB_PAREN_CODE = re.compile(r"\s*\([A-Z0-9][A-Z0-9\-_]{5,}\)")
_SUB_REF_SLASH_TAIL = re.compile(r"\s*/\s*Ref[:.]?\s*\S+\s*$", re.IGNORECASE)
_SUB_REF_PAREN = re.compile(r"\s*\(\s*Ref[:.]?\s*\S+?\s*\)", re.IGNORECASE)

_SUB_TRAIL_PLATFORM = re.compile(r"\s*[-–]\s*Studentjob\.ch\b.*$", re.IGNORECASE)
_SUB_TRAIL_COUNTRY_CODE = re.compile(r"_(?:CH|DE|AT|IT|FR|EU)\b\s*$")

_SUB_EMPTY_PAREN = re.compile(r"\s*\(\s*\)")
_SUB_DANGLING_OPEN = re.compile(r"\(\s*[;,\s]+")
_SUB_DANGLING_CLOSE = re.compile(r"[;,\s]+\)")
_SUB_TRAILING_PUNCT = re.compile(r"\s*[,–\-;]\s*$")

# Acronyms forced to uppercase after cleaning (receiver expects "CRM" not "Crm")
_SUB_ACRONYMS = frozenset({
    "CRM", "SAP", "D365", "FSM", "BI", "CE", "ERP", "CPQ", "SQL", "KPI",
    "B2B", "B2C", "SaaS", "UX", "UI", "QA", "IT", "HR", "PM", "AI",
    "API", "SEO", "SEM", "PPC", "CMS", "ETL", "DWH", "HPE", "EMEA",
    "UHNW", "AML", "KYC", "ESG",
})

_SUB_MAX_LEN = 65  # truncate at word boundary if longer


def _balance_parens(t: str) -> str:
    """If '(' count ≠ ')' count, trim from the last orphan '(' onward."""
    opens, closes = t.count("("), t.count(")")
    if opens > closes:
        depth = closes
        for i in range(len(t) - 1, -1, -1):
            if t[i] == "(":
                if depth == 0:
                    return t[:i].rstrip(" ,;-–")
                depth -= 1
    elif closes > opens:
        while t.count(")") > t.count("("):
            t = t.replace(")", "", 1)
    return t


def clean_subtitle(title: str) -> str:
    """Clean a raw job title for use as cover-letter / CV header subtitle.

    Strips gender markers, percentages, contract-type tails, reference codes,
    and zero-width chars. Preserves legitimate role context (product names,
    hyphenated specializations). Truncates at word boundary if > 65 chars.
    Returns "" on empty input so callers can fall back to a default.
    """
    t = (title or "").strip()
    if not t:
        return ""

    t = _SUB_ZERO_WIDTH.sub("", t)

    t = _SUB_GENDER_IN_COMPOUND.sub("", t)
    t = _SUB_GENDER_PAREN.sub("", t)
    t = _SUB_GENDER_BARE.sub("", t)
    t = _SUB_GENDER_SUFFIX.sub("", t)

    t = _SUB_PAREN_CODE.sub("", t)
    t = _SUB_REF_PAREN.sub("", t)
    t = _SUB_REF_SLASH_TAIL.sub("", t)

    t = _SUB_PENSUM.sub("", t)
    t = _SUB_PERCENT_PAREN_RANGE.sub("", t)
    t = _SUB_PERCENT_PAREN_SINGLE.sub("", t)
    t = _SUB_PERCENT_RANGE_PCT.sub("", t)
    t = _SUB_PERCENT_RANGE_BARE.sub("", t)
    t = _SUB_PERCENT_ANYWHERE.sub("", t)

    t = _SUB_CONTRACT_SPLIT.sub("", t)
    t = _SUB_TRAIL_PLATFORM.sub("", t)
    t = _SUB_TRAIL_COUNTRY_CODE.sub("", t)

    t = _SUB_DANGLING_OPEN.sub("(", t)
    t = _SUB_DANGLING_CLOSE.sub(")", t)
    t = _SUB_EMPTY_PAREN.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _balance_parens(t)
    t = _SUB_TRAILING_PUNCT.sub("", t).strip()

    # Uppercase known acronyms (preserving surrounding punctuation)
    def _fix_case(tok: str) -> str:
        m = re.match(r"^([^\w]*)([\w&.]+)([^\w]*)$", tok)
        if not m:
            return tok
        pre, core, post = m.groups()
        if core.upper() in _SUB_ACRONYMS:
            return pre + core.upper() + post
        return tok

    t = " ".join(_fix_case(w) for w in t.split(" "))

    if len(t) > _SUB_MAX_LEN:
        cut = t[:_SUB_MAX_LEN].rsplit(" ", 1)[0]
        t = cut + "…"

    return t


# --- enforce_revops_subtitle: brand-anchor enforcement for CV/CL header ---
# The candidate's functional title is "Revenue Operations Specialist" (untranslated,
# consistent across DE/EN/IT). Only the suffix (skill keywords) is dynamic per job.
# This helper guarantees the prefix regardless of LLM output drift.

_REVOPS_PREFIX = "Revenue Operations Specialist"
_REVOPS_SEPARATOR = " | "
_REVOPS_FALLBACK_SUFFIX = "CRM • SAP • Power BI"
_REVOPS_DRIFT_RE = re.compile(
    r"^Revenue\s+Operations\s+(?:Spezialist(?:in)?|Specialista|Spécialiste)",
    re.IGNORECASE,
)


def find_application_folder(applications_dir: Path, job_id: str) -> Optional[Path]:
    """Return the existing application folder for `job_id` under `applications_dir`, if any.

    Folders are named `{job_id}_{safe_company}_{safe_title}/` so a `{job_id}_*`
    glob is sufficient. Returns the first match (alphabetical) or None.

    Used as a fourth dedup safety net in CL/CV generators: if the checkpoint
    file was lost but the volume still has the artifacts, we should not
    regenerate them.
    """
    if not applications_dir.exists():
        return None
    matches = sorted(applications_dir.glob(f"{job_id}_*"))
    return matches[0] if matches else None


def has_cover_letter_outputs(folder: Path) -> bool:
    """True if `folder` contains both Cover_Letter_*.pdf and Cover_Letter_*.docx."""
    return any(folder.glob("Cover_Letter_*.pdf")) and any(folder.glob("Cover_Letter_*.docx"))


def has_cv_outputs(folder: Path) -> bool:
    """True if `folder` contains all four CV artifacts: fancy + ATS, PDF + DOCX."""
    pdfs = list(folder.glob("CV_*.pdf"))
    docxs = list(folder.glob("CV_*.docx"))
    has_fancy_pdf = any("_ATS" not in p.name for p in pdfs)
    has_fancy_docx = any("_ATS" not in d.name for d in docxs)
    has_ats_pdf = any("_ATS" in p.name for p in pdfs)
    has_ats_docx = any("_ATS" in d.name for d in docxs)
    return has_fancy_pdf and has_fancy_docx and has_ats_pdf and has_ats_docx


def enforce_revops_subtitle(raw: str | None, fallback_suffix: str = _REVOPS_FALLBACK_SUFFIX) -> str:
    """Guarantee subtitle starts with 'Revenue Operations Specialist | '.

    - If raw already has the prefix → returned as-is.
    - German/Italian/French translations of the prefix → normalized to English.
    - If raw lacks the prefix → treated as suffix, prefix prepended.
    - If raw is empty/None → fallback suffix used.
    """
    if not raw or not raw.strip():
        return f"{_REVOPS_PREFIX}{_REVOPS_SEPARATOR}{fallback_suffix}"
    cleaned = raw.strip()
    cleaned = _REVOPS_DRIFT_RE.sub(_REVOPS_PREFIX, cleaned)
    if cleaned.lower().startswith(_REVOPS_PREFIX.lower()):
        return cleaned
    suffix = cleaned.lstrip("|•·-—–:; ").strip()
    if not suffix:
        return f"{_REVOPS_PREFIX}{_REVOPS_SEPARATOR}{fallback_suffix}"
    return f"{_REVOPS_PREFIX}{_REVOPS_SEPARATOR}{suffix}"
