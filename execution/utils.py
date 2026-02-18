"""
Shared utilities — consolidated from generate_cover_letter.py and generate_cv.py.

Usage:
    from execution.utils import sanitize_filename, strip_legal_suffixes, normalize_company_key, clean_job_title
"""

from __future__ import annotations

import hashlib
import re


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
    """Strip URLs, percentage ranges, and pipe-separated suffixes from job titles.

    Useful for folder names, subject lines, and display contexts where
    job-board noise should be removed.
    """
    # Remove URLs (www.xxx.ch, https://...)
    title = re.sub(r'https?://\S+|www\.\S+', '', title)
    # Remove percentage ranges like "80-100%" or "80–100%"
    title = re.sub(r'\d{2,3}\s*[\u2013-]\s*\d{2,3}\s*%', '', title)
    # Remove standalone percentages like "100%"
    title = re.sub(r'\b\d{2,3}\s*%', '', title)
    # Remove pipe-separated suffixes (e.g., "| Meilen (ZH) | www.qsome.ch")
    title = re.sub(r'\s*\|.*$', '', title)
    # Clean up leftover whitespace and trailing punctuation
    return re.sub(r'\s+', ' ', title).strip().rstrip(' |\u2013-')
