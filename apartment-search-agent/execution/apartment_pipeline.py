#!/usr/bin/env python3
"""Local apartment listing tracker, scorer, and draft generator.

This script intentionally does not scrape portals or send applications. It
normalizes manually supplied listings or alert text, dedupes them in SQLite,
scores them against the Root D4 search strategy, and creates personalized
German application drafts for Simon to approve.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import textwrap
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "data" / "listings.sqlite"
DEFAULT_QUEUE_CSV = BASE_DIR / "data" / "application_queue.csv"
DEFAULT_DRAFTS_MD = BASE_DIR / "data" / "message_drafts.md"
WORK_ADDRESS = os.getenv("WORK_ADDRESS", "Platz 4, 6039 Root D4, Switzerland")

MAX_RENT_CHF = 1000
IDEAL_RENT_CHF = 800
DEFAULT_DAILY_LIMIT = 10
DEFAULT_SITE_DAILY_LIMIT = 3

RENT_KEYWORDS = [
    "miete",
    "mietzins",
    "mietpreis",
    "monatsmiete",
    "rent",
    "bruttomiete",
    "nettomiete",
    "warmmiete",
    "monatlich",
    "monthly",
    "price",
    "preis",
]

NON_RENT_MONEY_KEYWORDS = [
    "kaution",
    "deposit",
    "depot",
    "security",
    "garantie",
    "nebenkosten",
    "nk",
    "moebel",
    "furniture",
    "abstand",
    "fee",
]

GENERIC_TITLES = {
    "",
    "untitled listing",
    "wg zimmer",
    "zimmer",
    "room",
    "wohnung",
    "apartment",
    "studio",
}


COMMUTE_ESTIMATES: dict[str, tuple[int, str]] = {
    "root d4": (8, "e-bike/walk"),
    "root": (12, "e-bike/OeV"),
    "gisikon-root": (14, "e-bike/OeV"),
    "gisikon": (14, "e-bike/OeV"),
    "honau": (16, "e-bike"),
    "dierikon": (18, "e-bike/OeV"),
    "buchrain": (22, "e-bike/OeV"),
    "ebikon": (27, "OeV/e-bike"),
    "rotkreuz": (28, "OeV/e-bike"),
    "perlen": (30, "e-bike/OeV"),
    "inwil": (32, "e-bike"),
    "adligenswil": (35, "e-bike/OeV"),
    "luzern": (35, "OeV"),
    "lucerne": (35, "OeV"),
    "emmenbruecke": (40, "OeV"),
    "emmenbrucke": (40, "OeV"),
    "emmen": (42, "OeV"),
    "kriens": (50, "OeV"),
}

PRIMARY_LOCATIONS = {
    "root d4",
    "root",
    "gisikon-root",
    "gisikon",
    "honau",
    "dierikon",
    "buchrain",
    "ebikon",
    "rotkreuz",
}

WG_POSITIVE_PATTERNS = {
    "calm": [r"\bruhig\w*", r"\bcalm\b"],
    "clean": [r"\bsauber\w*", r"\bordentlich\w*", r"\bclean\b", r"\btidy\b"],
    "professionals": [r"berufstaetig\w*", r"young professional"],
    "social": [r"gemeinsam\w*", r"kochen", r"gesellig", r"social", r"unkompliziert"],
    "furnished": [r"moebliert", r"furnished"],
    "anmeldung": [r"anmeldung", r"registration"],
    "bike": [r"velo", r"fahrrad", r"e-?bike", r"bike"],
}

WG_NEGATIVE_PATTERNS = {
    "party": [r"party[- ]?wg", r"\bparty\b", r"feiern"],
    "unclear_contract": [r"ohne vertrag", r"no contract"],
    "no_viewing": [r"keine besichtigung", r"without viewing", r"no viewing"],
    "pressure": [r"sofort entscheiden", r"urgent payment", r"pay now"],
}

HARD_GENDER_PATTERNS = [
    r"\bnur frauen\b",
    r"\bonly women\b",
    r"\bfemale only\b",
    r"\bfrauen[- ]?wg\b",
    r"\bweibliche mitbewohnerin gesucht\b",
    r"\bnur weiblich\b",
]

SOFT_GENDER_PATTERNS = [
    r"\bfrauen bevorzugt\b",
    r"\bfrau bevorzugt\b",
    r"\bideally female\b",
    r"\bpreferably female\b",
    r"\bbevorzugt weiblich\b",
]

NO_ANMELDUNG_PATTERNS = [
    r"\bkeine anmeldung\b",
    r"\banmeldung nicht moeglich\b",
    r"\bohne anmeldung\b",
    r"\bno registration\b",
    r"\bwithout registration\b",
]

TEMPORARY_FALLBACK_PATTERNS = [
    r"\bzwischenmiete\b",
    r"\bbefristet\b",
    r"\btemporary\b",
    r"\bsublet\b",
    r"\buntervermietung\b",
]

LIMITED_USE_PATTERNS = [
    r"\bgelegenheit\w*nutzung\b",
    r"\bgelegentliche\w* nutzung\b",
    r"\bnicht fuer eine dauerhafte vollzeitbelegung\b",
    r"\bnicht fuer dauerhafte vollzeitbelegung\b",
    r"\beinzelne tage pro monat\b",
    r"\bonly present a few days per month\b",
    r"\bnot available for permanent full-time occupancy\b",
    r"\bweekly stays may also be possible\b",
    r"\bwochenaufenthalter\b",
]

SCAM_PATTERNS = {
    "payment_before_viewing": [
        r"zahlung vor besichtigung",
        r"deposit before viewing",
        r"kaution vor besichtigung",
        r"pay before viewing",
    ],
    "remote_key_shipping": [
        r"schluessel.*(post|versand)",
        r"key.*(shipping|courier|post)",
        r"landlord.*abroad",
        r"vermieter.*ausland",
    ],
    "no_viewing": [r"keine besichtigung", r"no viewing", r"without viewing"],
    "pressure": [r"sofort.*ueberweisen", r"decide immediately"],
    "furniture_buy_in": [r"moebel.*abkaufen", r"buy furniture"],
}

QUERY_PARAMS_TO_DROP = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


@dataclass(frozen=True)
class ListingInput:
    url: str | None
    source: str | None
    title: str | None
    rent_chf: int | None
    city: str | None
    move_in: str | None
    contact_name: str | None
    contact_email: str | None
    raw_text: str
    commute_minutes: int | None


@dataclass(frozen=True)
class ScoredListing:
    normalized: dict[str, Any]
    decision: str
    recommended_action: str
    priority_score: int
    commute_class: str
    commute_minutes: int | None
    commute_mode: str
    price_score: str
    wg_fit_score: int
    gender_status: str
    scam_risk: str
    flags: list[str]
    message_variant: str
    message_draft: str


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_for_match(value: str | None) -> str:
    value = normalize_space(value).lower()
    replacements = {
        "\u00e4": "ae",
        "\u00f6": "oe",
        "\u00fc": "ue",
        "\u00e9": "e",
        "\u00e8": "e",
        "\u00e0": "a",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    return value


def canonicalize_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()

    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in QUERY_PARAMS_TO_DROP
    ]
    query.sort(key=lambda item: (item[0].lower(), item[1]))
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def detect_source(url: str | None, source: str | None) -> str:
    if source:
        return source.strip()
    if not url:
        return "manual"
    hostname = urlsplit(url).netloc.lower()
    hostname = hostname[4:] if hostname.startswith("www.") else hostname
    return hostname or "manual"


def parse_money_amount(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    amount = int(digits)
    if 250 <= amount <= 5000:
        return amount
    return None


def money_amounts_in_order(text: str) -> list[tuple[int, int, int]]:
    pattern = re.compile(
        r"(?:(?:chf|fr\.?|sfr\.?)\s*([0-9][0-9' ]{1,7})|([0-9][0-9' ]{1,7})\s*(?:chf|fr\.?|sfr\.?))",
        re.IGNORECASE,
    )
    amounts: list[tuple[int, int, int]] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in pattern.finditer(text):
        amount = parse_money_amount(match.group(1) or match.group(2) or "")
        if amount is None:
            continue
        span = match.span()
        if span in seen_spans:
            continue
        seen_spans.add(span)
        amounts.append((amount, span[0], span[1]))
    return amounts


def contains_any_keyword(text: str, keywords: list[str]) -> bool:
    normalized = normalize_for_match(text)
    return any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords)


def extract_rent(raw_text: str) -> int | None:
    text = normalize_for_match(raw_text)
    amount_pattern = r"(?:(?:chf|fr\.?|sfr\.?)\s*([0-9][0-9' ]{1,7})|([0-9][0-9' ]{1,7})\s*(?:chf|fr\.?|sfr\.?))"
    explicit_patterns = [
        rf"(?:{'|'.join(RENT_KEYWORDS)})[^0-9]{{0,35}}{amount_pattern}",
        rf"{amount_pattern}[^A-Za-z0-9]{{0,25}}(?:pro monat|/monat|monatlich|monthly|miete|rent)",
    ]
    for pattern in explicit_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            amount = parse_money_amount(next(group for group in match.groups() if group))
            if amount is not None:
                return amount

    for amount, start, end in money_amounts_in_order(text):
        context_start = max(0, start - 45)
        context_end = min(len(text), end + 45)
        context = text[context_start:context_end]
        if contains_any_keyword(context, NON_RENT_MONEY_KEYWORDS) and not contains_any_keyword(context, RENT_KEYWORDS):
            continue
        return amount
    return None


def extract_city(raw_text: str) -> str | None:
    normalized = normalize_for_match(raw_text)
    for city in sorted(COMMUTE_ESTIMATES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(normalize_for_match(city))}\b", normalized):
            return city.title()

    swiss_postcode = re.search(r"\b60[0-9]{2}\s+([A-Za-z\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00e9\u00e8\u00e0\- ]{2,30})", raw_text)
    if swiss_postcode:
        return normalize_space(swiss_postcode.group(1))
    return None


def extract_move_in(raw_text: str) -> str | None:
    patterns = [
        r"(?:ab|from|frei ab|available from)\s*([0-3]?[0-9][./][0-1]?[0-9](?:[./][0-9]{2,4})?)",
        r"([0-3]?[0-9][./][0-1]?[0-9](?:[./][0-9]{2,4})?)",
        r"(?:mid|mitte)\s*juli",
        r"16\.?07\.?",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_text, re.IGNORECASE)
        if match:
            return match.group(1) if match.groups() else match.group(0)
    return None


def extract_email(raw_text: str) -> str | None:
    match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", raw_text)
    return match.group(0) if match else None


def extract_title(raw_text: str) -> str:
    for line in raw_text.splitlines():
        cleaned = normalize_space(line)
        if cleaned and len(cleaned) <= 120:
            return cleaned
    return "Untitled listing"


def parse_listing_input(args: argparse.Namespace) -> ListingInput:
    raw_parts: list[str] = []
    if args.text:
        raw_parts.append(args.text)
    if args.text_file:
        raw_parts.append(Path(args.text_file).read_text(encoding="utf-8"))
    if args.stdin:
        raw_parts.append(sys.stdin.read())

    raw_text = "\n".join(part for part in raw_parts if part).strip()
    if not raw_text and not args.url and not args.title:
        raise SystemExit("Provide --url, --title, --text, --text-file, or --stdin.")

    return ListingInput(
        url=args.url,
        source=args.source,
        title=args.title,
        rent_chf=args.rent,
        city=args.city,
        move_in=args.move_in,
        contact_name=args.contact_name,
        contact_email=args.contact_email,
        raw_text=raw_text,
        commute_minutes=args.commute_minutes,
    )


def canonical_key(normalized: dict[str, Any]) -> str:
    if normalized["canonical_url"]:
        return "url:" + normalized["canonical_url"]

    if normalized.get("content_key"):
        return "listing:" + normalized["content_key"]

    return "raw:" + normalized["raw_hash"][:24]


def content_key(normalized: dict[str, Any]) -> str:
    title = normalize_for_match(str(normalized.get("title") or ""))
    city = normalize_for_match(str(normalized.get("city") or ""))
    rent = str(normalized.get("rent_chf") or "")
    if not title or title in GENERIC_TITLES or not city or not rent:
        return ""

    parts = [
        title,
        city,
        rent,
        normalize_for_match(str(normalized.get("move_in") or "")),
    ]
    return hash_text("|".join(parts))[:24]


def normalize_listing(listing: ListingInput) -> dict[str, Any]:
    canonical_url = canonicalize_url(listing.url)
    raw_text = listing.raw_text.strip()
    title = normalize_space(listing.title) or extract_title(raw_text)
    rent = listing.rent_chf if listing.rent_chf is not None else extract_rent(raw_text)
    city = normalize_space(listing.city) or extract_city(raw_text)
    move_in = normalize_space(listing.move_in) or extract_move_in(raw_text)
    contact_email = normalize_space(listing.contact_email) or extract_email(raw_text)
    source = detect_source(canonical_url or listing.url, listing.source)
    normalized = {
        "url": listing.url or "",
        "canonical_url": canonical_url,
        "source": source,
        "title": title,
        "rent_chf": rent,
        "city": city or "",
        "move_in": move_in or "",
        "contact_name": normalize_space(listing.contact_name),
        "contact_email": contact_email or "",
        "raw_text": raw_text,
        "raw_hash": hash_text(raw_text or f"{canonical_url}|{title}|{rent}|{city}"),
        "commute_minutes_override": listing.commute_minutes,
    }
    normalized["content_key"] = content_key(normalized)
    normalized["canonical_key"] = canonical_key(normalized)
    return normalized


def any_pattern(patterns: list[str], text: str) -> bool:
    normalized_text = normalize_for_match(text)
    return any(re.search(pattern, normalized_text, re.IGNORECASE) for pattern in patterns)


def matching_pattern_keys(pattern_map: dict[str, list[str]], text: str) -> list[str]:
    return [key for key, patterns in pattern_map.items() if any_pattern(patterns, text)]


def _live_commute_lookup(city: str) -> tuple[int, str] | None:
    """Live ORS lookup, no-op when no API key is configured. Imported lazily
    so the static path keeps working without dotenv / network / sqlite3 setup."""
    try:
        from execution.commute_scoring import is_enabled, live_commute_minutes
    except Exception:  # noqa: BLE001 - never let scoring break on import errors
        return None
    if not is_enabled():
        return None
    query = city if "," in city else f"{city}, Switzerland"
    try:
        result = live_commute_minutes(query)
    except Exception:  # noqa: BLE001 - network/HTTP/SQLite errors never abort scoring
        return None
    if result is None:
        return None
    return result.minutes, result.mode


def estimate_commute(city: str, override_minutes: int | None = None) -> tuple[int | None, str, str]:
    if override_minutes is not None:
        minutes = override_minutes
        mode = "manual override"
    else:
        live = _live_commute_lookup(city) if city else None
        if live is not None:
            minutes, mode = live
        else:
            normalized_city = normalize_for_match(city)
            minutes = None
            mode = "unknown"
            for candidate, estimate in sorted(COMMUTE_ESTIMATES.items(), key=lambda item: len(item[0]), reverse=True):
                if normalize_for_match(candidate) in normalized_city:
                    minutes, mode = estimate
                    break

    if minutes is None:
        return None, "unknown", mode
    if minutes < 20:
        return minutes, "A+", mode
    if minutes <= 30:
        return minutes, "A", mode
    if minutes <= 45:
        return minutes, "B", mode
    return minutes, "C", mode


def classify_price(rent_chf: int | None, commute_class: str) -> str:
    if rent_chf is None:
        return "unknown"
    if rent_chf < IDEAL_RENT_CHF:
        return "excellent"
    if rent_chf <= 900:
        return "good"
    if rent_chf <= MAX_RENT_CHF and commute_class in {"A+", "A"}:
        return "acceptable"
    if rent_chf <= MAX_RENT_CHF:
        return "borderline"
    return "weak"


def classify_gender(text: str) -> str:
    if any_pattern(HARD_GENDER_PATTERNS, text):
        return "hard_exclusion"
    if any_pattern(SOFT_GENDER_PATTERNS, text):
        return "manual_review"
    return "eligible"


def scam_risk_from_flags(flags: list[str]) -> str:
    high_flags = {"payment_before_viewing", "remote_key_shipping", "no_viewing"}
    if high_flags.intersection(flags):
        return "high"
    if flags:
        return "medium"
    return "low"


def compute_wg_fit(text: str) -> tuple[int, list[str]]:
    positive = matching_pattern_keys(WG_POSITIVE_PATTERNS, text)
    negative = matching_pattern_keys(WG_NEGATIVE_PATTERNS, text)
    score = 50 + len(positive) * 7 - len(negative) * 12
    score = max(0, min(100, score))
    flags = [f"positive:{item}" for item in positive] + [f"negative:{item}" for item in negative]
    return score, flags


def listing_type(text: str) -> str:
    normalized = normalize_for_match(text)
    if any(token in normalized for token in ["wg", "zimmer", "room", "mitbewohner"]):
        return "wg"
    if any(token in normalized for token in ["studio", "wohnung", "apartment", "1-zimmer"]):
        return "apartment"
    return "listing"


def priority_from_scores(
    rent_chf: int | None,
    commute_class: str,
    wg_fit_score: int,
    scam_risk: str,
    gender_status: str,
    city: str,
) -> int:
    score = 0
    score += {"A+": 45, "A": 35, "B": 15, "C": -20, "unknown": 0}.get(commute_class, 0)
    if rent_chf is not None:
        if rent_chf < 800:
            score += 25
        elif rent_chf <= 900:
            score += 15
        elif rent_chf <= 1000:
            score += 5
        else:
            score -= 40
    score += int((wg_fit_score - 50) * 0.4)
    if normalize_for_match(city) in PRIMARY_LOCATIONS:
        score += 8
    if scam_risk == "medium":
        score -= 25
    elif scam_risk == "high":
        score -= 60
    if gender_status == "manual_review":
        score -= 15
    elif gender_status == "hard_exclusion":
        score -= 100
    return max(0, min(100, score))


def decide_action(
    rent_chf: int | None,
    commute_class: str,
    gender_status: str,
    scam_risk: str,
    priority_score: int,
    flags: list[str],
) -> tuple[str, str]:
    if gender_status == "hard_exclusion":
        return "skip", "Skip: hard gender restriction excludes Simon."
    if scam_risk == "high":
        return "skip", "Skip: high scam/process risk."
    if "limited_use_only" in flags:
        return "skip", "Skip: listing is only for occasional/limited use."
    if rent_chf is None:
        return "manual_review", "Manual review: rent could not be parsed."
    if rent_chf is not None and rent_chf > MAX_RENT_CHF:
        return "skip", "Skip unless Simon explicitly approves over-budget listing."
    if "no_anmeldung" in flags:
        return "skip", "Skip: Anmeldung is not possible."
    if "no_anmeldung_temporary" in flags:
        return "manual_review", "Manual review: temporary listing without Anmeldung."
    if "negative:unclear_contract" in flags:
        return "manual_review", "Manual review: contract terms are unclear."
    if gender_status == "manual_review":
        return "manual_review", "Manual review: soft female-preference wording."
    if commute_class == "C":
        return "skip", "Skip unless emergency: commute is over 45 minutes."
    if commute_class in {"A+", "A"} and priority_score >= 60:
        return "apply", "Apply after Simon approval; strong commute/value fit."
    if commute_class == "B" and priority_score >= 55:
        return "consider", "Consider if queue is thin or WG fit is strong."
    if commute_class == "unknown":
        return "manual_review", "Manual review: commute could not be estimated."
    return "consider", "Consider after stronger A+/A listings."


def seeded_choice(options: list[str], seed: str) -> str:
    rng = random.Random(hash_text(seed))
    return options[rng.randrange(len(options))]


def personalization_sentence(normalized: dict[str, Any], flags: list[str], commute_class: str) -> str:
    city = normalized.get("city") or "der Gegend"
    move_in = normalized.get("move_in") or "Mitte Juli"
    pieces: list[str] = []
    if commute_class in {"A+", "A"}:
        pieces.append(
            f"Die Lage in {city} ist fuer mich besonders spannend, weil ich ab dem 16.07. bei PHENOGY in Root D4 starte."
        )
    else:
        pieces.append(
            f"{city} koennte fuer mich gut passen, wenn die Verbindung nach Root D4 im Alltag verlaesslich ist."
        )
    if "positive:furnished" in flags:
        pieces.append("Dass das Zimmer moebliert ist, waere fuer den Start ab Mitte Juli sehr praktisch.")
    if "positive:anmeldung" in flags:
        pieces.append("Gut ist fuer mich auch, dass Anmeldung/saubere Vertragslage im Inserat erkennbar ist.")
    if "positive:bike" in flags:
        pieces.append("Ein Platz fuer Velo oder E-Bike waere fuer meinen Arbeitsweg ein echter Pluspunkt.")
    if normalized.get("move_in"):
        pieces.append(f"Der angegebene Einzugstermin {move_in} passt gut zu meinem Start.")
    return " ".join(pieces[:2])


def generate_message(
    normalized: dict[str, Any],
    flags: list[str],
    gender_status: str,
    commute_class: str,
) -> tuple[str, str]:
    text = "\n".join(
        str(normalized.get(field) or "")
        for field in ("title", "city", "move_in", "raw_text")
    )
    kind = listing_type(text)
    city = normalized.get("city") or "eurer Gegend"
    contact_name = normalized.get("contact_name") or ""
    greeting = f"Hoi {contact_name}".strip() if contact_name else "Hoi zaeme"
    personalization = personalization_sentence(normalized, flags, commute_class)
    budget_sentence = (
        "Das Budget aus dem Inserat passt fuer mich."
        if normalized.get("rent_chf") and int(normalized["rent_chf"]) <= MAX_RENT_CHF
        else "Zum Budget kann ich mich gerne direkt am Inserat orientieren."
    )

    seed = normalized["canonical_key"] + "|" + normalized.get("raw_hash", "")
    opener = seeded_choice(
        [
            f"Ich habe euer Angebot in {city} gesehen und es wirkt fuer mich sehr passend.",
            f"Das Inserat in {city} hat mich direkt angesprochen.",
            f"Euer Angebot in {city} klingt nach einer sehr guten Option fuer meinen Start in der Schweiz.",
        ],
        seed + ":opener",
    )
    wg_fit = seeded_choice(
        [
            "In einer WG bin ich ordentlich, unkompliziert und ruecksichtsvoll.",
            "Mir ist wichtig, dass gemeinsame Raeume sauber bleiben und man entspannt miteinander umgehen kann.",
            "Ich bin eher ruhig und verlaesslich, aber gerne auch sozial bei einem Essen oder Gespraech.",
        ],
        seed + ":fit",
    )
    viewing = seeded_choice(
        [
            "Falls es fuer euch passt, komme ich gerne kurzfristig zur Besichtigung vorbei oder telefoniere zuerst kurz.",
            "Ich wuerde euch sehr gerne kennenlernen und bin fuer eine Besichtigung zeitlich flexibel.",
            "Gerne koennen wir kurz telefonieren/videochatten und danach eine Besichtigung abmachen.",
        ],
        seed + ":viewing",
    )

    if gender_status == "manual_review":
        variant = "women_preferred_manual_review"
        body = f"""
        {greeting}

        Ich habe gesehen, dass ihr eher eine Frau sucht. Falls ihr trotzdem offen seid: {opener} {personalization}

        Kurz zu mir: Ich bin 23, starte am 16.07. bei PHENOGY in Root D4 und komme aus dem Bereich BESS / erneuerbare Energien. {wg_fit} Ich suche keine Party-WG, sondern ein ruhiges, sauberes und unkompliziertes Zuhause.

        Falls es trotzdem fuer euch denkbar ist, wuerde ich euch gerne kurz kennenlernen.

        Liebe Gruesse
        Simon
        """
    elif kind == "apartment":
        variant = "apartment"
        body = f"""
        {greeting}

        {opener} {personalization}

        Kurz zu mir: Ich bin Simon, 23, und starte am 16.07. bei PHENOGY in Root D4 im Bereich BESS / erneuerbare Energien. Ich bin ruhig, sauber, zuverlaessig und suche eine praktische Wohnung fuer meinen Arbeitsalltag mit OeV/E-Bike.

        {budget_sentence} {viewing}

        Liebe Gruesse
        Simon
        """
    else:
        variant = "wg"
        body = f"""
        {greeting}

        {opener} {personalization}

        Kurz zu mir: Ich bin 23, komme aus dem Bereich BESS / erneuerbare Energien und interessiere mich fuer Energie, Technik und Nachhaltigkeit. {wg_fit} Ich bin gerne sozial und trinke auch mal gemuetlich etwas mit, suche aber keine Party-WG.

        {budget_sentence} {viewing}

        Liebe Gruesse
        Simon
        """

    message = textwrap.dedent(body).strip()
    return variant, message


def score_listing(normalized: dict[str, Any]) -> ScoredListing:
    combined_text = "\n".join(
        str(normalized.get(field) or "")
        for field in ("title", "city", "move_in", "raw_text")
    )
    minutes, commute_class, mode = estimate_commute(
        str(normalized.get("city") or ""),
        normalized.get("commute_minutes_override"),
    )
    gender_status = classify_gender(combined_text)
    scam_flags = matching_pattern_keys(SCAM_PATTERNS, combined_text)
    scam_risk = scam_risk_from_flags(scam_flags)
    wg_fit_score, wg_flags = compute_wg_fit(combined_text)
    if any_pattern(NO_ANMELDUNG_PATTERNS, combined_text):
        wg_flags = [flag for flag in wg_flags if flag != "positive:anmeldung"]
        if any_pattern(TEMPORARY_FALLBACK_PATTERNS, combined_text):
            wg_flags.append("no_anmeldung_temporary")
        else:
            wg_flags.append("no_anmeldung")
            wg_fit_score = max(0, wg_fit_score - 20)
    if any_pattern(LIMITED_USE_PATTERNS, combined_text):
        wg_flags.append("limited_use_only")
        wg_fit_score = max(0, wg_fit_score - 35)
    price_score = classify_price(normalized.get("rent_chf"), commute_class)
    flags = sorted(set(scam_flags + wg_flags))

    priority_score = priority_from_scores(
        normalized.get("rent_chf"),
        commute_class,
        wg_fit_score,
        scam_risk,
        gender_status,
        str(normalized.get("city") or ""),
    )
    decision, recommended_action = decide_action(
        normalized.get("rent_chf"),
        commute_class,
        gender_status,
        scam_risk,
        priority_score,
        flags,
    )
    message_variant, message_draft = generate_message(
        normalized,
        flags,
        gender_status,
        commute_class,
    )
    return ScoredListing(
        normalized=normalized,
        decision=decision,
        recommended_action=recommended_action,
        priority_score=priority_score,
        commute_class=commute_class,
        commute_minutes=minutes,
        commute_mode=mode,
        price_score=price_score,
        wg_fit_score=wg_fit_score,
        gender_status=gender_status,
        scam_risk=scam_risk,
        flags=flags,
        message_variant=message_variant,
        message_draft=message_draft,
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL UNIQUE,
            content_key TEXT,
            canonical_url TEXT,
            url TEXT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            rent_chf INTEGER,
            city TEXT,
            move_in TEXT,
            contact_name TEXT,
            contact_email TEXT,
            raw_text TEXT,
            raw_hash TEXT NOT NULL,
            decision TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            priority_score INTEGER NOT NULL,
            commute_class TEXT NOT NULL,
            commute_minutes INTEGER,
            commute_mode TEXT,
            price_score TEXT NOT NULL,
            wg_fit_score INTEGER NOT NULL,
            gender_status TEXT NOT NULL,
            scam_risk TEXT NOT NULL,
            flags_json TEXT NOT NULL,
            message_variant TEXT NOT NULL,
            message_draft TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            approval_status TEXT NOT NULL DEFAULT 'not_requested',
            approved_at TEXT,
            approved_raw_hash TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (listing_id) REFERENCES listings(id)
        );

        CREATE INDEX IF NOT EXISTS idx_listings_queue
            ON listings(decision, status, approval_status, priority_score);
        CREATE INDEX IF NOT EXISTS idx_listings_source_sent
            ON listings(source, sent_at);
        """
    )
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(listings)").fetchall()
    }
    if "content_key" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN content_key TEXT")
    if "approved_raw_hash" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN approved_raw_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_content_key ON listings(content_key)"
    )
    conn.commit()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["flags"] = json.loads(result.pop("flags_json") or "[]")
    return result


def upsert_listing(conn: sqlite3.Connection, scored: ScoredListing) -> tuple[int, bool]:
    init_db(conn)
    n = scored.normalized
    timestamp = now_iso()
    payload = {
        "canonical_key": n["canonical_key"],
        "content_key": n["content_key"],
        "canonical_url": n["canonical_url"],
        "url": n["url"],
        "source": n["source"],
        "title": n["title"],
        "rent_chf": n["rent_chf"],
        "city": n["city"],
        "move_in": n["move_in"],
        "contact_name": n["contact_name"],
        "contact_email": n["contact_email"],
        "raw_text": n["raw_text"],
        "raw_hash": n["raw_hash"],
        "decision": scored.decision,
        "recommended_action": scored.recommended_action,
        "priority_score": scored.priority_score,
        "commute_class": scored.commute_class,
        "commute_minutes": scored.commute_minutes,
        "commute_mode": scored.commute_mode,
        "price_score": scored.price_score,
        "wg_fit_score": scored.wg_fit_score,
        "gender_status": scored.gender_status,
        "scam_risk": scored.scam_risk,
        "flags_json": json.dumps(scored.flags, ensure_ascii=True),
        "message_variant": scored.message_variant,
        "message_draft": scored.message_draft,
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    existing = conn.execute(
        """
        SELECT id, sent_at, raw_hash, decision, approval_status
        FROM listings
        WHERE canonical_key = ?
           OR (content_key IS NOT NULL AND content_key != '' AND content_key = ?)
        ORDER BY CASE WHEN canonical_key = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (n["canonical_key"], n["content_key"], n["canonical_key"]),
    ).fetchone()
    if existing:
        reset_approval = (
            existing["approval_status"] == "approved"
            and not existing["sent_at"]
            and (
                existing["raw_hash"] != n["raw_hash"]
                or existing["decision"] != scored.decision
                or scored.decision not in {"apply", "consider"}
            )
        )
        conn.execute(
            """
            UPDATE listings
            SET canonical_key = :canonical_key,
                content_key = :content_key,
                canonical_url = :canonical_url,
                url = :url,
                source = :source,
                title = :title,
                rent_chf = :rent_chf,
                city = :city,
                move_in = :move_in,
                contact_name = :contact_name,
                contact_email = :contact_email,
                raw_text = :raw_text,
                raw_hash = :raw_hash,
                decision = :decision,
                recommended_action = :recommended_action,
                priority_score = :priority_score,
                commute_class = :commute_class,
                commute_minutes = :commute_minutes,
                commute_mode = :commute_mode,
                price_score = :price_score,
                wg_fit_score = :wg_fit_score,
                gender_status = :gender_status,
                scam_risk = :scam_risk,
                flags_json = :flags_json,
                message_variant = :message_variant,
                message_draft = :message_draft,
                approval_status = CASE
                    WHEN :reset_approval = 1 THEN 'not_requested'
                    ELSE approval_status
                END,
                approved_at = CASE
                    WHEN :reset_approval = 1 THEN NULL
                    ELSE approved_at
                END,
                approved_raw_hash = CASE
                    WHEN :reset_approval = 1 THEN NULL
                    ELSE approved_raw_hash
                END,
                status = CASE
                    WHEN sent_at IS NOT NULL THEN status
                    WHEN :reset_approval = 1 THEN 'new'
                    ELSE status
                END,
                updated_at = :updated_at
            WHERE id = :existing_id
            """,
            {
                **payload,
                "existing_id": int(existing["id"]),
                "reset_approval": 1 if reset_approval else 0,
            },
        )
        listing_id = int(existing["id"])
        created = False
    else:
        conn.execute(
            """
            INSERT INTO listings (
                canonical_key, content_key, canonical_url, url, source, title, rent_chf,
                city, move_in, contact_name, contact_email, raw_text, raw_hash,
                decision, recommended_action, priority_score, commute_class,
                commute_minutes, commute_mode, price_score, wg_fit_score,
                gender_status, scam_risk, flags_json, message_variant,
                message_draft, created_at, updated_at
            )
            VALUES (
                :canonical_key, :content_key, :canonical_url, :url, :source, :title, :rent_chf,
                :city, :move_in, :contact_name, :contact_email, :raw_text, :raw_hash,
                :decision, :recommended_action, :priority_score, :commute_class,
                :commute_minutes, :commute_mode, :price_score, :wg_fit_score,
                :gender_status, :scam_risk, :flags_json, :message_variant,
                :message_draft, :created_at, :updated_at
            )
            """,
            payload,
        )
        listing_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        created = True

    conn.execute(
        """
        INSERT INTO application_events(listing_id, event_type, note, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            listing_id,
            "ingested" if created else "updated",
            scored.recommended_action,
            timestamp,
        ),
    )
    if existing and reset_approval:
        conn.execute(
            """
            INSERT INTO application_events(listing_id, event_type, note, created_at)
            VALUES (?, 'approval_reset', ?, ?)
            """,
            (
                listing_id,
                "Approval reset because listing content or decision changed.",
                timestamp,
            ),
        )
    conn.commit()
    return listing_id, created


def fetch_listing(conn: sqlite3.Connection, listing_id: int) -> sqlite3.Row:
    init_db(conn)
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Listing {listing_id} not found.")
    return row


def list_queue(conn: sqlite3.Connection, limit: int, include_skip: bool = False) -> list[dict[str, Any]]:
    init_db(conn)
    decisions = ("apply", "consider", "manual_review") if not include_skip else ("apply", "consider", "manual_review", "skip")
    placeholders = ",".join("?" for _ in decisions)
    rows = conn.execute(
        f"""
        SELECT *
        FROM listings
        WHERE decision IN ({placeholders})
          AND status NOT IN ('sent', 'archived')
        ORDER BY
          CASE decision
            WHEN 'apply' THEN 0
            WHEN 'consider' THEN 1
            WHEN 'manual_review' THEN 2
            ELSE 3
          END,
          priority_score DESC,
          commute_minutes IS NULL,
          commute_minutes ASC,
          rent_chf IS NULL,
          rent_chf ASC
        LIMIT ?
        """,
        (*decisions, limit),
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def site_send_counts_last_24h(conn: sqlite3.Connection) -> dict[str, int]:
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    rows = conn.execute(
        """
        SELECT source, COUNT(*) AS count
        FROM listings
        WHERE sent_at >= ?
        GROUP BY source
        """,
        (cutoff,),
    ).fetchall()
    return {str(row["source"]): int(row["count"]) for row in rows}


def safe_daily_plan(
    conn: sqlite3.Connection,
    daily_limit: int,
    site_daily_limit: int,
) -> list[dict[str, Any]]:
    queue = list_queue(conn, limit=max(daily_limit * 3, daily_limit), include_skip=False)
    site_counts = site_send_counts_last_24h(conn)
    selected: list[dict[str, Any]] = []

    for item in queue:
        if item["decision"] != "apply":
            continue
        source = item["source"]
        if site_counts.get(source, 0) >= site_daily_limit:
            continue
        selected.append(item)
        site_counts[source] = site_counts.get(source, 0) + 1
        if len(selected) >= daily_limit:
            break
    return selected


def approve_listing(conn: sqlite3.Connection, listing_id: int, note: str | None) -> None:
    row = fetch_listing(conn, listing_id)
    if row["decision"] not in {"apply", "consider"}:
        raise SystemExit(
            f"Listing {listing_id} has decision '{row['decision']}', so it needs manual review before approval."
        )
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE listings
        SET approval_status = 'approved',
            approved_at = ?,
            approved_raw_hash = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, row["raw_hash"], timestamp, listing_id),
    )
    conn.execute(
        """
        INSERT INTO application_events(listing_id, event_type, note, created_at)
        VALUES (?, 'approved', ?, ?)
        """,
        (listing_id, note or "Approved by Simon for manual sending.", timestamp),
    )
    conn.commit()


def mark_sent(conn: sqlite3.Connection, listing_id: int, note: str | None, force: bool = False) -> None:
    row = fetch_listing(conn, listing_id)
    if not force:
        if row["decision"] not in {"apply", "consider"}:
            raise SystemExit(
                f"Listing {listing_id} has decision '{row['decision']}' and cannot be marked sent without --force."
            )
        if row["gender_status"] != "eligible" or row["scam_risk"] != "low":
            raise SystemExit(
                f"Listing {listing_id} needs manual review before sending (gender={row['gender_status']}, scam={row['scam_risk']})."
            )
        if row["approval_status"] != "approved":
            raise SystemExit(
                f"Listing {listing_id} is not approved. Run approve first, or pass --force if Simon already sent it manually."
            )
        if row["approved_raw_hash"] != row["raw_hash"]:
            raise SystemExit(
                f"Listing {listing_id} changed after approval. Re-review and approve it again before marking sent."
            )
    if row["sent_at"]:
        raise SystemExit(f"Listing {listing_id} is already marked sent at {row['sent_at']}.")
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE listings
        SET status = 'sent',
            sent_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (timestamp, timestamp, listing_id),
    )
    conn.execute(
        """
        INSERT INTO application_events(listing_id, event_type, note, created_at)
        VALUES (?, 'sent', ?, ?)
        """,
        (listing_id, note or "Marked as sent.", timestamp),
    )
    conn.commit()


def export_queue(conn: sqlite3.Connection, csv_path: Path, drafts_path: Path, limit: int) -> None:
    queue = list_queue(conn, limit=limit, include_skip=False)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    drafts_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "id",
        "decision",
        "priority_score",
        "source",
        "title",
        "rent_chf",
        "city",
        "move_in",
        "commute_class",
        "commute_minutes",
        "commute_mode",
        "price_score",
        "wg_fit_score",
        "gender_status",
        "scam_risk",
        "recommended_action",
        "url",
        "contact_email",
        "approval_status",
        "status",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in queue:
            writer.writerow({field: item.get(field, "") for field in fieldnames})

    with drafts_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Message Drafts\n\n")
        handle.write("Generated drafts. Send only after Simon approves the listing.\n\n")
        for item in queue:
            handle.write(f"## Listing {item['id']}: {item['title']}\n\n")
            handle.write(f"- Decision: {item['decision']}\n")
            handle.write(f"- Priority: {item['priority_score']}\n")
            handle.write(f"- Source: {item['source']}\n")
            handle.write(f"- Rent: CHF {item['rent_chf'] or 'unknown'}\n")
            handle.write(f"- City: {item['city'] or 'unknown'}\n")
            handle.write(f"- Commute: {item['commute_class']} ({item['commute_minutes'] or 'unknown'} min, {item['commute_mode']})\n")
            handle.write(f"- Action: {item['recommended_action']}\n")
            if item.get("url"):
                handle.write(f"- URL: {item['url']}\n")
            handle.write("\n```text\n")
            handle.write(item["message_draft"].strip())
            handle.write("\n```\n\n")


def print_listing_summary(listing_id: int, created: bool, scored: ScoredListing) -> None:
    status = "created" if created else "updated"
    n = scored.normalized
    print(f"Listing {listing_id} {status}: {n['title']}")
    print(f"Decision: {scored.decision} | priority {scored.priority_score} | {scored.recommended_action}")
    print(f"Rent: CHF {n.get('rent_chf') or 'unknown'} | city: {n.get('city') or 'unknown'}")
    print(f"Commute: {scored.commute_class} ({scored.commute_minutes or 'unknown'} min, {scored.commute_mode})")
    print(f"Gender: {scored.gender_status} | scam risk: {scored.scam_risk}")
    if scored.flags:
        print("Flags: " + ", ".join(scored.flags))
    print("\nDraft:\n")
    print(scored.message_draft)


def print_queue(queue: list[dict[str, Any]]) -> None:
    if not queue:
        print("No active listings in queue.")
        return
    for item in queue:
        rent = f"CHF {item['rent_chf']}" if item["rent_chf"] else "rent unknown"
        commute = (
            f"{item['commute_class']} / {item['commute_minutes']} min"
            if item["commute_minutes"] is not None
            else f"{item['commute_class']} / unknown"
        )
        print(
            f"#{item['id']} [{item['decision']}] score={item['priority_score']} "
            f"{rent} | {item['city'] or 'unknown'} | {commute} | {item['title']}"
        )
        print(f"  {item['recommended_action']}")
        if item.get("url"):
            print(f"  {item['url']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track, score, and draft Swiss WG/apartment applications near Root D4."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create or migrate the local SQLite tracker.")

    ingest = subparsers.add_parser("ingest", help="Ingest a manually supplied listing or alert text.")
    ingest.add_argument("--url")
    ingest.add_argument("--source")
    ingest.add_argument("--title")
    ingest.add_argument("--rent", type=int)
    ingest.add_argument("--city")
    ingest.add_argument("--move-in")
    ingest.add_argument("--contact-name")
    ingest.add_argument("--contact-email")
    ingest.add_argument("--commute-minutes", type=int, help="Manual door-to-door commute estimate.")
    ingest.add_argument("--text")
    ingest.add_argument("--text-file")
    ingest.add_argument("--stdin", action="store_true", help="Read listing text from stdin.")

    queue = subparsers.add_parser("queue", help="Show the ranked application queue.")
    queue.add_argument("--limit", type=int, default=20)
    queue.add_argument("--include-skip", action="store_true")

    plan = subparsers.add_parser("daily-plan", help="Show a compliant low-volume send plan.")
    plan.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT)
    plan.add_argument("--site-daily-limit", type=int, default=DEFAULT_SITE_DAILY_LIMIT)

    export = subparsers.add_parser("export", help="Export queue CSV and message drafts.")
    export.add_argument("--limit", type=int, default=50)
    export.add_argument("--csv", type=Path, default=DEFAULT_QUEUE_CSV)
    export.add_argument("--drafts", type=Path, default=DEFAULT_DRAFTS_MD)

    approve = subparsers.add_parser("approve", help="Record Simon's approval for a listing.")
    approve.add_argument("listing_id", type=int)
    approve.add_argument("--note")

    sent = subparsers.add_parser("mark-sent", help="Mark an approved listing as manually sent.")
    sent.add_argument("listing_id", type=int)
    sent.add_argument("--note")
    sent.add_argument("--force", action="store_true", help="Use only if Simon already sent it manually.")

    show = subparsers.add_parser("show", help="Show one listing and its draft.")
    show.add_argument("listing_id", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    with closing(connect(args.db)) as conn:
        if args.command == "init-db":
            init_db(conn)
            print(f"Initialized tracker at {args.db}")
            return 0

        if args.command == "ingest":
            listing = parse_listing_input(args)
            scored = score_listing(normalize_listing(listing))
            listing_id, created = upsert_listing(conn, scored)
            print_listing_summary(listing_id, created, scored)
            return 0

        if args.command == "queue":
            print_queue(list_queue(conn, args.limit, args.include_skip))
            return 0

        if args.command == "daily-plan":
            selected = safe_daily_plan(conn, args.daily_limit, args.site_daily_limit)
            print_queue(selected)
            print(
                "\nNo messages were sent. Approve each listing first, then send manually and run mark-sent."
            )
            return 0

        if args.command == "export":
            export_queue(conn, args.csv, args.drafts, args.limit)
            print(f"Exported queue to {args.csv}")
            print(f"Exported drafts to {args.drafts}")
            return 0

        if args.command == "approve":
            approve_listing(conn, args.listing_id, args.note)
            print(f"Approved listing {args.listing_id}.")
            return 0

        if args.command == "mark-sent":
            mark_sent(conn, args.listing_id, args.note, args.force)
            print(f"Marked listing {args.listing_id} as sent.")
            return 0

        if args.command == "show":
            item = row_to_dict(fetch_listing(conn, args.listing_id))
            print_queue([item])
            print("\nDraft:\n")
            print(item["message_draft"])
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
