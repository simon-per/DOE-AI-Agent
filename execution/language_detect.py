"""
Consolidated language detection — used by cover letter, CV, and follow-up stages.

Supports German, English, and Italian (no French per user preference).

Strategy:
1. langdetect library on description (high confidence for long text)
2. langdetect on title (less reliable but useful)
3. Keyword fallback for short/ambiguous text
4. Default to German (most Swiss job postings)

Usage:
    from execution.language_detect import detect_language

    lang_code, lang_name = detect_language(title, description)
    # Returns e.g. ("de", "German"), ("en", "English"), ("it", "Italian")
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

try:
    from langdetect import detect as _langdetect_detect
    from langdetect import LangDetectException as _LangDetectException
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0  # Make detection deterministic on short text
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False
    _LangDetectException = Exception

SUPPORTED_LANGUAGES = {
    "de": ("de", "German"),
    "en": ("en", "English"),
    "it": ("it", "Italian"),
}


def detect_language(title: str, description: str) -> tuple[str, str]:
    """Detect job posting language from title + description.

    Returns (language_code, language_name).
    """
    desc_clean = (description or "").strip()
    has_description = desc_clean and desc_clean != "nan" and len(desc_clean) > 50

    # Strategy 1: Use langdetect on description (high confidence for long text)
    if _HAS_LANGDETECT and has_description:
        try:
            detected = _langdetect_detect(desc_clean)
            if detected in SUPPORTED_LANGUAGES:
                return SUPPORTED_LANGUAGES[detected]
        except _LangDetectException:
            pass

    # Strategy 2: Use langdetect on title (less reliable but still useful)
    if _HAS_LANGDETECT and title and len(title) > 15:
        try:
            detected = _langdetect_detect(title)
            if detected in SUPPORTED_LANGUAGES:
                return SUPPORTED_LANGUAGES[detected]
        except _LangDetectException:
            pass

    # Strategy 3: Keyword fallback for short/ambiguous text
    text_lower = f"{title} {desc_clean}".lower()

    de_words = ["und", "für", "wir", "ihre", "aufgaben", "anforderungen",
                "erfahrung", "kenntnisse", "stelle", "bewerbung", "verantwortlich"]
    en_words = ["and", "you", "the", "our", "requirements", "experience",
                "skills", "responsibilities", "apply", "candidate"]
    it_words = ["per", "nostro", "compiti", "requisiti", "esperienza",
                "competenze", "lavoro", "azienda", "candidato",
                "dei", "gestione", "processi"]

    de_count = sum(1 for w in de_words if f" {w} " in f" {text_lower} ")
    en_count = sum(1 for w in en_words if f" {w} " in f" {text_lower} ")
    it_count = sum(1 for w in it_words if f" {w} " in f" {text_lower} ")

    scores = {"de": de_count, "en": en_count, "it": it_count}
    best = max(scores, key=scores.get)

    if scores[best] > 0:
        return SUPPORTED_LANGUAGES[best]

    # Default to German (most Swiss job postings)
    log.warning("Language detection inconclusive (no keywords matched) — defaulting to German")
    return ("de", "German")
