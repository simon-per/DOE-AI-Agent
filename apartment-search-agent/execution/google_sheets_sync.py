#!/usr/bin/env python3
"""Publish the local apartment tracker to Google Sheets.

SQLite remains the source of truth. This script mirrors selected tracker views
to a user-owned Google Sheet so Simon can review listings and approve actions
from a familiar interface.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import time
import traceback
from contextlib import closing
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
PARENT_DIR = BASE_DIR.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "listings.sqlite"
DEFAULT_SOURCE_REGISTRY = BASE_DIR / "data" / "source_registry.csv"
DEFAULT_SHEET_NAME = "Swiss Appartment Search Pipeline"
DEFAULT_OAUTH_CREDENTIALS_PATH = PARENT_DIR / "credentials.json"
DEFAULT_OAUTH_TOKEN_PATH = BASE_DIR / "token.json"
DEFAULT_PARENT_OAUTH_TOKEN_PATH = PARENT_DIR / "token.json"

PIPELINE_SHEET = "Pipeline"
LISTINGS_SHEET = "Listings"
QUEUE_SHEET = "Queue"
APPLICATIONS_SHEET = "Applications"
SOURCES_SHEET = "Sources"
SUMMARY_SHEET = "Summary"
SETTINGS_SHEET = "Settings"
DEFAULT_BLANK_SHEET_TITLES = {"Sheet1", "Tabellenblatt1"}
LEGACY_GENERATED_SHEETS = {LISTINGS_SHEET, QUEUE_SHEET, APPLICATIONS_SHEET}

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

LISTING_COLUMNS = [
    "listing_id",
    "decision",
    "status",
    "approval_status",
    "total_score",
    "priority",
    "source",
    "source_url",
    "original_url",
    "title",
    "category_raw",
    "category_normalized",
    "price_chf",
    "price_includes_utilities",
    "deposit_chf",
    "available_from",
    "move_in_match",
    "address_raw",
    "city",
    "zip",
    "distance_km_to_root_d4",
    "pt_commute_min",
    "bike_commute_min",
    "commute_class",
    "best_commute_min",
    "commute_mode",
    "price_score",
    "price_score_numeric",
    "location_score",
    "availability_score",
    "wg_fit_score",
    "listing_quality_score",
    "red_flag_penalty",
    "gender_status",
    "scam_risk",
    "flags",
    "skip_reason",
    "recommended_action",
    "contact_type",
    "contact_name",
    "contact_email",
    "url",
    "message_variant",
    "message_hash",
    "description_excerpt",
    "first_seen",
    "last_seen",
    "is_active",
    "duplicate_of",
    "created_at",
    "updated_at",
    "sent_at",
    "openrouter_score",
    "openrouter_reason",
]

QUEUE_COLUMNS = [
    "rank",
    "listing_id",
    "decision",
    "total_score",
    "source",
    "title",
    "city",
    "price_chf",
    "best_commute_min",
    "commute_class",
    "why_relevant",
    "needs_simon_approval",
    "approved_by_simon",
    "application_status",
    "application_channel",
    "contact_name",
    "contact_email",
    "contact_phone",
    "available_from",
    "recommended_action",
    "url",
    "message_variant",
    "message_draft",
]

APPLICATION_GENERATED_COLUMNS = [
    "listing_id",
    "active_in_queue",
    "rank",
    "source",
    "title",
    "city",
    "price_chf",
    "best_commute_min",
    "total_score",
    "why_relevant",
    "url",
    "message_draft",
]

APPLICATION_MANUAL_COLUMNS = [
    "application_status",
    "simon_approval",
    "final_message",
    "sent_at",
    "follow_up_date",
    "response_status",
    "response_notes",
    "next_action",
    "last_manual_update",
]

APPLICATION_COLUMNS = APPLICATION_GENERATED_COLUMNS + APPLICATION_MANUAL_COLUMNS

PIPELINE_COLUMNS = [
    # Core — the at-a-glance decision columns (job-pipeline style: relevant first)
    "status",
    "score",
    "title",
    "city",
    "price_chf",
    "commute_class",
    "best_commute_min",
    "available_from",
    "source",
    "url",
    # Secondary — context "if you care"
    "listing_id",
    "system_decision",
    "category",
    "wg_fit_score",
    "risk",
    "flags",
    "system_reason",
    "openrouter_score",
    "message_draft",
    # Manual tracking — edited by Simon, preserved across syncs
    "sent_at",
    "follow_up_date",
    "response_status",
    "response_notes",
    "next_action",
]

PIPELINE_MANUAL_COLUMNS = [
    "status",
    "simon_approval",
    "final_message",
    "sent_at",
    "follow_up_date",
    "response_status",
    "response_notes",
    "next_action",
    "last_manual_update",
]

# Manual Status vocabulary, mirrored from the job pipeline
# (../execution/write_jobs_to_sheet.py). Simon edits Status in the Sheet; the
# value is preserved across daily syncs via merge_pipeline_values. The system
# only ever sets the derived states (New/Applied/Expired/Irrelevant) — the rest
# (Interviewing/Offer/Rejected/Duplicate/No_Response) are Simon's to set.
STATUS_VALUES = [
    "New",
    "Applied",
    "Interviewing",
    "Offer",
    "Rejected",
    "Irrelevant",
    "Duplicate",
    "Expired",
    "No_Response",
]

# RGB (0..1) per status, reusing the job sheet's color scheme.
STATUS_COLORS: dict[str, tuple[float, float, float]] = {
    "New": (0.80, 0.90, 1.00),          # light blue
    "Applied": (0.56, 0.74, 0.96),      # blue
    "Interviewing": (0.72, 0.88, 0.73), # green
    "Offer": (0.40, 0.85, 0.50),        # bright green
    "Rejected": (0.96, 0.70, 0.70),     # red
    "Irrelevant": (0.87, 0.87, 0.87),   # grey
    "Duplicate": (1.00, 0.76, 0.38),    # orange
    "Expired": (0.80, 0.80, 0.80),      # grey
    "No_Response": (0.93, 0.93, 0.93),  # light grey
}

# priority_score is 0..100 (apartment_pipeline.score_listing); apply needs >=60
# with a strong commute, consider >=55. Color bands for the score column.
SCORE_GREEN_MIN = 70
SCORE_YELLOW_MIN = 50

SOURCE_COLUMNS = [
    "source",
    "display_name",
    "base_url",
    "priority",
    "coverage",
    "ingestion_method",
    "automation_status",
    "notes",
]


def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"Tracker database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def json_list(value: str | None) -> str:
    if not value:
        return ""
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(data, list):
        return ", ".join(str(item) for item in data)
    return str(data)


def cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def normalize_for_match(value: str | None) -> str:
    if not value:
        return ""
    normalized = re.sub(r"\s+", " ", value).strip().lower()
    replacements = {
        "\u00e4": "ae",
        "\u00f6": "oe",
        "\u00fc": "ue",
        "\u00e9": "e",
        "\u00e8": "e",
        "\u00e0": "a",
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
    return normalized


def listing_text(row: dict[str, Any]) -> str:
    return "\n".join(
        str(row.get(field) or "")
        for field in ("title", "city", "move_in", "raw_text")
    )


def category_normalized(row: dict[str, Any]) -> str:
    text = normalize_for_match(listing_text(row))
    if "limited_use_only" in str(row.get("flags") or ""):
        return "skip"
    if any(token in text for token in ("wg", "zimmer", "room", "mitbewohner")):
        return "WG room"
    if any(token in text for token in ("studio", "1-zimmer", "1 zimmer")):
        return "studio"
    if any(token in text for token in ("wohnung", "apartment", "appartment", "flat")):
        return "apartment"
    return "unknown"


def price_includes_utilities(raw_text: str | None) -> str:
    text = normalize_for_match(raw_text)
    if any(token in text for token in ("exkl. nebenkosten", "exklusive nebenkosten", "nk exkl", "without utilities")):
        return "no"
    if any(token in text for token in ("inkl. nebenkosten", "inklusive nebenkosten", "warmmiete", "utilities included", "charges included")):
        return "yes"
    return ""


def amount_near_keyword(raw_text: str | None, keywords: tuple[str, ...]) -> int | str:
    text = raw_text or ""
    if not text:
        return ""
    keyword_pattern = "|".join(re.escape(keyword) for keyword in keywords)
    patterns = [
        rf"(?:{keyword_pattern}).{{0,40}}?(?:chf|fr\.?|sfr\.?)\s*([0-9][0-9' ]{{1,7}})",
        rf"(?:{keyword_pattern}).{{0,40}}?([0-9][0-9' ]{{1,7}})\s*(?:chf|fr\.?|sfr\.?)",
        rf"(?:chf|fr\.?|sfr\.?)\s*([0-9][0-9' ]{{1,7}}).{{0,40}}?(?:{keyword_pattern})",
        rf"([0-9][0-9' ]{{1,7}})\s*(?:chf|fr\.?|sfr\.?).{{0,40}}?(?:{keyword_pattern})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            amount = int(re.sub(r"[^0-9]", "", match.group(1)))
            if 100 <= amount <= 10000:
                return amount
    return ""


def move_in_match(move_in: str | None) -> str:
    text = normalize_for_match(move_in)
    if not text:
        return "unknown"
    if any(token in text for token in ("sofort", "immediately", "ab jetzt", "nach vereinbarung")):
        return "flexible_or_early"
    if any(token in text for token in ("16.07", "15.07", "juli", "july", "07.202", "2026-07")):
        return "target_match"
    return "check"


def price_score_numeric(rent_chf: int | None) -> int | str:
    if rent_chf is None:
        return ""
    if rent_chf < 800:
        return 100
    if rent_chf <= 900:
        return 80
    if rent_chf <= 1000:
        return 60
    return 0


def location_score(commute_class: str | None) -> int:
    return {"A+": 100, "A": 85, "B": 55, "C": 15, "unknown": 0}.get(commute_class or "unknown", 0)


def availability_score(move_in: str | None) -> int:
    match = move_in_match(move_in)
    if match == "target_match":
        return 100
    if match == "flexible_or_early":
        return 85
    if match == "check":
        return 60
    return 30


def red_flag_penalty(row: dict[str, Any]) -> int:
    if row.get("gender_status") == "hard_exclusion":
        return -100
    if row.get("scam_risk") == "high":
        return -80
    flags = str(row.get("flags") or "")
    if "limited_use_only" in flags:
        return -80
    if row.get("gender_status") == "manual_review":
        return -25
    if row.get("scam_risk") == "medium":
        return -25
    return 0


def short_excerpt(raw_text: str | None, max_chars: int = 300) -> str:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def message_hash(message: str | None) -> str:
    if not message:
        return ""
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


def contact_type(row: dict[str, Any]) -> str:
    if row.get("contact_email"):
        return "email"
    if row.get("url"):
        return "portal"
    return "unknown"


def application_channel(row: dict[str, Any]) -> str:
    source = str(row.get("source") or "")
    if row.get("contact_email"):
        return "email"
    if source:
        return source
    return "manual"


def why_relevant(row: dict[str, Any]) -> str:
    pieces: list[str] = []
    commute_class = row.get("commute_class") or "unknown"
    if commute_class in {"A+", "A"}:
        pieces.append(f"{commute_class} commute")
    elif commute_class == "B":
        pieces.append("acceptable commute")
    rent = row.get("rent_chf")
    if rent:
        pieces.append(f"CHF {rent}")
    if row.get("wg_fit_score") not in (None, ""):
        pieces.append(f"WG fit {row['wg_fit_score']}/100")
    if row.get("recommended_action"):
        pieces.append(str(row["recommended_action"]))
    return " | ".join(pieces)


def skip_reason(row: dict[str, Any]) -> str:
    if row.get("decision") == "skip":
        return str(row.get("recommended_action") or row.get("flags") or "")
    return ""


def enrich_listing_row(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    rent = row.get("rent_chf")
    commute_minutes = row.get("commute_minutes")
    mode = normalize_for_match(str(row.get("commute_mode") or ""))
    enriched.update(
        {
            "listing_id": row.get("id"),
            "total_score": row.get("priority_score"),
            "priority": row.get("decision"),
            "source_url": row.get("canonical_url") or row.get("url"),
            "original_url": row.get("url"),
            "category_raw": "",
            "category_normalized": category_normalized(row),
            "price_chf": rent,
            "price_includes_utilities": price_includes_utilities(row.get("raw_text")),
            "deposit_chf": amount_near_keyword(row.get("raw_text"), ("kaution", "deposit", "depot")),
            "available_from": row.get("move_in"),
            "move_in_match": move_in_match(row.get("move_in")),
            "address_raw": "",
            "zip": "",
            "distance_km_to_root_d4": "",
            "pt_commute_min": commute_minutes if "oev" in mode or "bus" in mode or "train" in mode else "",
            "bike_commute_min": commute_minutes if "bike" in mode else "",
            "best_commute_min": commute_minutes,
            "price_score_numeric": price_score_numeric(rent),
            "location_score": location_score(row.get("commute_class")),
            "availability_score": availability_score(row.get("move_in")),
            "listing_quality_score": row.get("wg_fit_score"),
            "red_flag_penalty": red_flag_penalty(row),
            "skip_reason": skip_reason(row),
            "contact_type": contact_type(row),
            "message_hash": message_hash(row.get("message_draft")),
            "description_excerpt": short_excerpt(row.get("raw_text")),
            "first_seen": row.get("created_at"),
            # Real liveness now: last_seen is stamped on every re-sighting, and a
            # listing the Flatfox liveness check expired (or that was sent/archived)
            # is no longer active.
            "last_seen": row.get("last_seen") or row.get("updated_at"),
            "is_active": "no" if (
                row.get("sent_at") or row.get("status") in ("archived", "expired")
            ) else "yes",
            "duplicate_of": "",
        }
    )
    return enriched


def enrich_queue_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
    enriched = enrich_listing_row(row)
    enriched.update(
        {
            "rank": rank,
            "needs_simon_approval": "yes" if row.get("decision") in {"apply", "consider", "manual_review"} else "no",
            "approved_by_simon": "yes" if row.get("approval_status") == "approved" else "no",
            "application_status": row.get("status"),
            "application_channel": application_channel(row),
            "contact_phone": "",
            "why_relevant": why_relevant(row),
        }
    )
    return enriched


def listing_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM listings
        ORDER BY
          CASE decision
            WHEN 'apply' THEN 0
            WHEN 'consider' THEN 1
            WHEN 'manual_review' THEN 2
            WHEN 'skip' THEN 3
            ELSE 4
          END,
          priority_score DESC,
          rent_chf IS NULL,
          rent_chf ASC
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["flags"] = json_list(item.pop("flags_json", ""))
        # Advisory LLM re-rank (execution/llm_rerank.py), shown when present.
        # Coerce NULLs to "" so the cells stay clean for un-scored rows.
        item["openrouter_score"] = "" if item.get("openrouter_score") is None else item["openrouter_score"]
        item["openrouter_reason"] = item.get("openrouter_reason") or ""
        result.append(enrich_listing_row(item))
    return result


def queue_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM listings
        WHERE decision IN ('apply', 'consider', 'manual_review')
          AND status NOT IN ('sent', 'archived', 'expired')
        ORDER BY
          CASE decision
            WHEN 'apply' THEN 0
            WHEN 'consider' THEN 1
            WHEN 'manual_review' THEN 2
            ELSE 3
          END,
          priority_score DESC,
          rent_chf IS NULL,
          rent_chf ASC
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        item = dict(row)
        item["flags"] = json_list(item.pop("flags_json", ""))
        result.append(enrich_queue_row(item, rank))
    return result


def source_rows(source_registry: Path) -> list[dict[str, Any]]:
    if not source_registry.exists():
        raise SystemExit(f"Source registry not found: {source_registry}")
    with source_registry.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in SOURCE_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(
                f"Source registry is missing required columns: {', '.join(missing)}"
            )
        return list(reader)


def summary_rows(conn: sqlite3.Connection) -> list[list[Any]]:
    rows: list[list[Any]] = [["metric", "value"]]
    scalar_queries = [
        ("total_listings", "SELECT COUNT(*) FROM listings"),
        ("apply", "SELECT COUNT(*) FROM listings WHERE decision = 'apply'"),
        ("consider", "SELECT COUNT(*) FROM listings WHERE decision = 'consider'"),
        ("manual_review", "SELECT COUNT(*) FROM listings WHERE decision = 'manual_review'"),
        ("skip", "SELECT COUNT(*) FROM listings WHERE decision = 'skip'"),
        ("sent", "SELECT COUNT(*) FROM listings WHERE status = 'sent'"),
    ]
    for label, query in scalar_queries:
        rows.append([label, conn.execute(query).fetchone()[0]])

    rows.append(["", ""])
    rows.append(["decision", "count"])
    for row in conn.execute("SELECT decision, COUNT(*) FROM listings GROUP BY decision ORDER BY COUNT(*) DESC"):
        rows.append([row[0], row[1]])

    rows.append(["", ""])
    rows.append(["city", "count"])
    for row in conn.execute("SELECT city, COUNT(*) FROM listings GROUP BY city ORDER BY COUNT(*) DESC, city LIMIT 25"):
        rows.append([row[0] or "unknown", row[1]])
    return rows


def application_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in queue_rows(conn):
        item = {column: row.get(column, "") for column in APPLICATION_GENERATED_COLUMNS}
        item["active_in_queue"] = "yes"
        item.update({column: "" for column in APPLICATION_MANUAL_COLUMNS})
        rows.append(item)
    return rows


def default_pipeline_status(row: dict[str, Any]) -> str:
    """Map the system decision/tracker status to the manual Status vocabulary
    (job-pipeline style). Only produces system-derived states; Simon promotes
    rows to Interviewing / Offer / Rejected / Duplicate / No_Response by hand."""
    tracker_status = str(row.get("status") or "")
    decision = str(row.get("decision") or "")
    if tracker_status == "expired":
        return "Expired"
    if tracker_status == "sent":
        return "Applied"
    if tracker_status == "archived":
        return "Irrelevant"
    if decision == "skip":
        return "Irrelevant"
    return "New"


def pipeline_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Relevant-only rows for the slim Pipeline tab: the actionable tiers, minus
    taken-down (expired) or archived listings. System 'skip' (women-only, scam,
    far, over-budget) never appears. Ordering is inherited from listing_rows
    (decision tier -> priority_score desc -> rent)."""
    rows: list[dict[str, Any]] = []
    for row in listing_rows(conn):
        if row.get("decision") not in ("apply", "consider", "manual_review"):
            continue
        if row.get("status") in ("expired", "archived"):
            continue
        item = {
            "status": default_pipeline_status(row),
            "score": row.get("total_score"),
            "title": row.get("title"),
            "city": row.get("city"),
            "price_chf": row.get("price_chf"),
            "commute_class": row.get("commute_class"),
            "best_commute_min": row.get("best_commute_min"),
            "available_from": row.get("available_from"),
            "source": row.get("source"),
            "url": row.get("url"),
            "listing_id": row.get("listing_id"),
            "system_decision": row.get("decision"),
            "category": row.get("category_normalized"),
            "wg_fit_score": row.get("wg_fit_score"),
            "risk": row.get("scam_risk"),
            "flags": row.get("flags"),
            "system_reason": row.get("recommended_action"),
            "openrouter_score": row.get("openrouter_score"),
            "message_draft": row.get("message_draft"),
            "sent_at": row.get("sent_at"),
            "follow_up_date": "",
            "response_status": "",
            "response_notes": "",
            "next_action": "",
        }
        rows.append(item)
    return rows


def settings_rows() -> list[list[Any]]:
    return [
        ["section", "field", "value", "notes"],
        ["target", "job_address", "Platz 4, 6039 Root D4, Switzerland", "Primary commute destination."],
        ["target", "move_in_target", "2026-07-16", "Mid-July move-in target."],
        ["budget", "ideal_budget_chf", 800, "Strong preference below this."],
        ["budget", "max_budget_chf", 1000, "Only above this with explicit Simon approval."],
        ["commute", "a_plus_max_minutes", 20, "Apply immediately if other constraints pass."],
        ["commute", "a_max_minutes", 30, "Apply same day if strong."],
        ["commute", "b_max_minutes", 45, "Use as fallback if price or WG fit is strong."],
        ["weights", "price", 25, "Current deterministic score component."],
        ["weights", "commute", 45, "Commute is weighted nearly like rent."],
        ["weights", "wg_fit", 20, "Derived from listing text signals."],
        ["weights", "risk_penalty", -100, "Hard exclusions override all scores."],
        ["safety", "auto_submit_applications", "no", "Simon must approve each listing first."],
        ["safety", "captcha_or_bot_bypass", "no", "Use official APIs, alerts, and low-volume review workflows."],
        ["messages", "language", "German default", "Keep Swiss WG tone: warm, concise, reliable."],
        ["messages", "dedupe_identical_messages", "message_hash", "Review hash before sending similar drafts."],
        ["openrouter", "enabled", "no", "Do not use paid scoring without explicit budget/model approval."],
    ]


def dicts_to_values(rows: list[dict[str, Any]], columns: list[str]) -> list[list[Any]]:
    values = [columns]
    for row in rows:
        values.append([cell_value(row.get(column)) for column in columns])
    return values


def values_to_dicts(values: list[list[Any]]) -> list[dict[str, Any]]:
    if not values:
        return []
    headers = [str(header) for header in values[0]]
    rows: list[dict[str, Any]] = []
    for value_row in values[1:]:
        item: dict[str, Any] = {}
        for index, header in enumerate(headers):
            item[header] = value_row[index] if index < len(value_row) else ""
        rows.append(item)
    return rows


def merge_application_values(
    generated_values: list[list[Any]],
    existing_values: list[list[Any]] | None,
) -> list[list[Any]]:
    """Preserve manual application rows/columns from an existing Sheet export."""
    if not existing_values:
        return generated_values

    existing_by_id: dict[str, dict[str, Any]] = {}
    for row in values_to_dicts(existing_values):
        listing_id = str(row.get("listing_id") or "").strip()
        if listing_id:
            existing_by_id[listing_id] = row

    headers = [str(header) for header in generated_values[0]]
    merged = [headers]
    generated_ids: set[str] = set()
    for generated_row in values_to_dicts(generated_values):
        listing_id = str(generated_row.get("listing_id") or "").strip()
        if listing_id:
            generated_ids.add(listing_id)
        existing = existing_by_id.get(listing_id, {})
        for column in APPLICATION_MANUAL_COLUMNS:
            if column in existing:
                generated_row[column] = existing[column]
        merged.append([cell_value(generated_row.get(column)) for column in headers])

    for listing_id, existing in existing_by_id.items():
        if listing_id in generated_ids:
            continue
        archived = {column: existing.get(column, "") for column in headers}
        archived["listing_id"] = listing_id
        archived["active_in_queue"] = "no"
        for column in APPLICATION_GENERATED_COLUMNS:
            if column in {"listing_id", "active_in_queue"}:
                continue
            archived.setdefault(column, existing.get(column, ""))
        merged.append([cell_value(archived.get(column)) for column in headers])
    return merged


def application_manual_values(values: list[list[Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in values_to_dicts(values):
        listing_id = str(row.get("listing_id") or "").strip()
        if not listing_id:
            continue
        result[listing_id] = {
            "status": row.get("application_status") or "",
            "simon_approval": row.get("simon_approval") or "",
            "final_message": row.get("final_message") or "",
            "sent_at": row.get("sent_at") or "",
            "follow_up_date": row.get("follow_up_date") or "",
            "response_status": row.get("response_status") or "",
            "response_notes": row.get("response_notes") or "",
            "next_action": row.get("next_action") or "",
            "last_manual_update": row.get("last_manual_update") or "",
        }
    return result


def pipeline_manual_values(values: list[list[Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in values_to_dicts(values):
        listing_id = str(row.get("listing_id") or "").strip()
        if not listing_id:
            continue
        result[listing_id] = {
            column: row.get(column, "")
            for column in PIPELINE_MANUAL_COLUMNS
            if column in row
        }
    return result


def has_manual_progress(manual_row: dict[str, Any]) -> bool:
    """True when Simon has actually tracked a row — its Status moved off the
    default 'New', or any other manual field is filled. Used to decide whether a
    row that's no longer in the generated (relevant) set is worth keeping."""
    status = str(manual_row.get("status") or "").strip()
    if status and status.lower() != "new":
        return True
    return any(
        str(manual_row.get(column) or "").strip()
        for column in PIPELINE_MANUAL_COLUMNS
        if column != "status"
    )


def merge_pipeline_values(
    generated_values: list[list[Any]],
    existing_pipeline_values: list[list[Any]] | None,
    existing_application_values: list[list[Any]] | None,
) -> list[list[Any]]:
    application_manual = application_manual_values(existing_application_values or [])
    pipeline_manual = pipeline_manual_values(existing_pipeline_values or [])

    headers = [str(header) for header in generated_values[0]]
    merged = [headers]
    generated_ids: set[str] = set()

    for generated_row in values_to_dicts(generated_values):
        listing_id = str(generated_row.get("listing_id") or "").strip()
        if listing_id:
            generated_ids.add(listing_id)
        for source in (application_manual.get(listing_id, {}), pipeline_manual.get(listing_id, {})):
            for column in PIPELINE_MANUAL_COLUMNS:
                if column in source and source[column] not in (None, ""):
                    generated_row[column] = source[column]
        merged.append([cell_value(generated_row.get(column)) for column in headers])

    for listing_id, manual_row in pipeline_manual.items():
        if listing_id in generated_ids:
            continue
        # The slim Pipeline hides system 'skip'/'expired' rows. Without this
        # guard, every such row that was ever synced would resurface here as an
        # 'archived' row, defeating the slimming. Only keep rows Simon actually
        # tracked (Status moved off New, or another manual field filled).
        if not has_manual_progress(manual_row):
            continue
        archived = {column: "" for column in headers}
        archived.update(manual_row)
        archived["listing_id"] = listing_id
        archived["status"] = archived.get("status") or "Expired"
        archived["system_decision"] = "missing_from_tracker"
        merged.append([cell_value(archived.get(column)) for column in headers])

    return merged


def _color(rgb: tuple[float, float, float]) -> dict[str, float]:
    red, green, blue = rgb
    return {"red": red, "green": green, "blue": blue}


def pipeline_format_targets() -> tuple[int, int, int]:
    """(header_len, status_col_index, score_col_index) for the Pipeline tab."""
    return (
        len(PIPELINE_COLUMNS),
        PIPELINE_COLUMNS.index("status"),
        PIPELINE_COLUMNS.index("score"),
    )


def build_pipeline_format_requests(
    sheet_id: int,
    header_len: int,
    num_rows: int,
    status_col: int,
    score_col: int,
    existing_rule_count: int,
) -> list[dict[str, Any]]:
    """Sheets API `batchUpdate` requests to make the Pipeline tab feel like the
    job sheet: frozen bold header, a Status dropdown, per-status colors, and
    green/yellow/red score bands. Idempotent — it first deletes the
    `existing_rule_count` conditional-format rules so daily re-syncs don't pile up."""
    data_end = max(num_rows + 1, 2)  # header is row 1; ensure a valid (non-empty) range
    requests: list[dict[str, Any]] = []

    # Clear existing conditional-format rules first (re-sync idempotency).
    for _ in range(max(existing_rule_count, 0)):
        requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})

    # Freeze the header row.
    requests.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })
    # Bold + grey header.
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": header_len,
            },
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    })
    # Status dropdown (non-strict so manual typos warn rather than reject).
    status_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": data_end,
        "startColumnIndex": status_col, "endColumnIndex": status_col + 1,
    }
    requests.append({
        "setDataValidation": {
            "range": status_range,
            "rule": {
                "condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": value} for value in STATUS_VALUES]},
                "showCustomUi": True,
                "strict": False,
            },
        }
    })

    # Score color bands.
    score_range = {
        "sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": data_end,
        "startColumnIndex": score_col, "endColumnIndex": score_col + 1,
    }
    score_bands = [
        ({"type": "NUMBER_GREATER_THAN_EQ", "values": [{"userEnteredValue": str(SCORE_GREEN_MIN)}]}, (0.72, 0.88, 0.73)),
        ({"type": "NUMBER_BETWEEN", "values": [
            {"userEnteredValue": str(SCORE_YELLOW_MIN)}, {"userEnteredValue": str(SCORE_GREEN_MIN - 1)},
        ]}, (1.0, 0.95, 0.6)),
        ({"type": "NUMBER_LESS", "values": [{"userEnteredValue": str(SCORE_YELLOW_MIN)}]}, (0.96, 0.7, 0.7)),
    ]
    for condition, rgb in score_bands:
        requests.append({"addConditionalFormatRule": {
            "rule": {"ranges": [score_range],
                     "booleanRule": {"condition": condition, "format": {"backgroundColor": _color(rgb)}}},
            "index": 0,
        }})

    # Per-status color rules.
    for status_text, rgb in STATUS_COLORS.items():
        requests.append({"addConditionalFormatRule": {
            "rule": {"ranges": [status_range],
                     "booleanRule": {
                         "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status_text}]},
                         "format": {"backgroundColor": _color(rgb)},
                     }},
            "index": 0,
        }})

    return requests


def existing_cf_rule_count(sheets_metadata: dict[str, Any], sheet_id: int) -> int:
    for sheet in sheets_metadata.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            return len(sheet.get("conditionalFormats", []) or [])
    return 0


def format_pipeline_gspread(spreadsheet: Any, pipeline_values: list[list[Any]]) -> None:
    """Apply Pipeline formatting via gspread (the daily-run OAuth path)."""
    worksheet = sheets_api_call(spreadsheet.worksheet, PIPELINE_SHEET)
    sheet_id = worksheet.id
    # Request conditionalFormats explicitly so existing_cf_rule_count is reliable
    # (a bare fetch_sheet_metadata may omit it) — keeps daily re-syncs idempotent
    # instead of piling up duplicate color rules. Mirrors format_pipeline_service.
    metadata = sheets_api_call(
        spreadsheet.fetch_sheet_metadata,
        params={"fields": "sheets(properties(sheetId,title),conditionalFormats)"},
    )
    header_len, status_col, score_col = pipeline_format_targets()
    requests = build_pipeline_format_requests(
        sheet_id, header_len, max(len(pipeline_values) - 1, 0), status_col, score_col,
        existing_cf_rule_count(metadata, sheet_id),
    )
    if requests:
        sheets_api_call(spreadsheet.batch_update, {"requests": requests})


def format_pipeline_service(service: Any, spreadsheet_id: str, pipeline_values: list[list[Any]]) -> None:
    """Apply Pipeline formatting via the legacy service-account path."""
    metadata = execute_google_request(
        service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title),conditionalFormats)",
        )
    )
    sheet_id = None
    for sheet in metadata.get("sheets", []):
        if sheet.get("properties", {}).get("title") == PIPELINE_SHEET:
            sheet_id = sheet["properties"].get("sheetId")
            break
    if sheet_id is None:
        return
    header_len, status_col, score_col = pipeline_format_targets()
    requests = build_pipeline_format_requests(
        sheet_id, header_len, max(len(pipeline_values) - 1, 0), status_col, score_col,
        existing_cf_rule_count(metadata, sheet_id),
    )
    if requests:
        execute_google_request(
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            )
        )


def build_workbook_values(
    conn: sqlite3.Connection,
    source_registry: Path,
) -> dict[str, list[list[Any]]]:
    return {
        PIPELINE_SHEET: dicts_to_values(pipeline_rows(conn), PIPELINE_COLUMNS),
        SOURCES_SHEET: dicts_to_values(source_rows(source_registry), SOURCE_COLUMNS),
        SUMMARY_SHEET: summary_rows(conn),
        SETTINGS_SHEET: settings_rows(),
    }


def import_oauth_libraries():
    try:
        import gspread
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "Google OAuth libraries are missing. Install gspread, google-auth, "
            "and google-auth-oauthlib."
        ) from exc
    return gspread, Request, Credentials, InstalledAppFlow


def token_file_has_required_scopes(token_path: Path) -> bool:
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    raw_scopes = token_data.get("scopes") or token_data.get("scope")
    if isinstance(raw_scopes, str):
        granted_scopes = set(raw_scopes.split())
    elif isinstance(raw_scopes, list):
        granted_scopes = {str(scope) for scope in raw_scopes}
    else:
        return False
    return set(GOOGLE_SCOPES).issubset(granted_scopes)


def load_google_client(
    credentials_path: Path,
    token_path: Path,
    source_token_path: Path | None = None,
):
    """Authenticate to Google Sheets with a repo-local token seeded from parent auth."""
    if not credentials_path.exists():
        raise SystemExit(f"Google OAuth credentials file not found: {credentials_path}")

    gspread, Request, Credentials, InstalledAppFlow = import_oauth_libraries()
    creds = None
    read_token_path = token_path
    if not read_token_path.exists() and source_token_path and source_token_path.exists():
        read_token_path = source_token_path

    token_has_required_scopes = False
    if read_token_path.exists():
        token_has_required_scopes = token_file_has_required_scopes(read_token_path)
        try:
            if token_has_required_scopes:
                creds = Credentials.from_authorized_user_file(str(read_token_path), GOOGLE_SCOPES)
        except Exception as exc:
            raise SystemExit(f"Could not load Google OAuth token {read_token_path}: {exc}") from exc

    has_required_scopes = token_has_required_scopes
    if creds and hasattr(creds, "has_scopes"):
        has_required_scopes = token_has_required_scopes and bool(creds.has_scopes(GOOGLE_SCOPES))

    if not creds or not creds.valid or not has_required_scopes:
        if creds and has_required_scopes and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                GOOGLE_SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    elif read_token_path != token_path and not token_path.exists():
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return gspread.authorize(creds)


def load_google_service(credentials_path: Path):
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit(
            "Google client libraries are missing. Install google-api-python-client and google-auth."
        ) from exc

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if not credentials_path.exists():
        raise SystemExit(f"Google credentials file not found: {credentials_path}")
    try:
        credentials = Credentials.from_service_account_file(str(credentials_path), scopes=scopes)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Could not load Google service account credentials: {exc}") from exc
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def execute_google_request(request: Any, max_tries: int = 5) -> Any:
    retryable_statuses = {429, 500, 502, 503}
    for attempt in range(max_tries):
        try:
            return request.execute()
        except Exception as exc:
            response = getattr(exc, "resp", None) or getattr(exc, "response", None)
            status = getattr(response, "status", None) or getattr(response, "status_code", None)
            if status not in retryable_statuses or attempt == max_tries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def existing_sheet_titles(service: Any, spreadsheet_id: str) -> set[str]:
    spreadsheet = execute_google_request(service.spreadsheets().get(spreadsheetId=spreadsheet_id))
    return {
        sheet["properties"]["title"]
        for sheet in spreadsheet.get("sheets", [])
    }


def ensure_sheets(service: Any, spreadsheet_id: str, sheet_names: list[str]) -> None:
    existing = existing_sheet_titles(service, spreadsheet_id)
    requests = [
        {"addSheet": {"properties": {"title": sheet_name}}}
        for sheet_name in sheet_names
        if sheet_name not in existing
    ]
    if requests:
        execute_google_request(
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": requests},
            )
        )


def sync_values(service: Any, spreadsheet_id: str, workbook: dict[str, list[list[Any]]]) -> None:
    sheet_names = ordered_sheet_names(workbook)
    ensure_sheets(service, spreadsheet_id, sheet_names)
    for sheet_name in sheet_names:
        values = workbook[sheet_name]
        escaped = sheet_name.replace("'", "''")
        execute_google_request(
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{escaped}'!A1",
                valueInputOption="RAW",
                body={"values": values},
            )
        )
        clear_start_row = len(values) + 1
        clear_ranges = [f"'{escaped}'!A{clear_start_row}:ZZ10000"]
        width = max(len(values[0]) if values else 1, 1)
        if values and width < 702:
            clear_ranges.append(f"'{escaped}'!{column_letter(width + 1)}1:ZZ{len(values)}")
        for clear_range in clear_ranges:
            execute_google_request(
                service.spreadsheets().values().clear(
                    spreadsheetId=spreadsheet_id,
                    range=clear_range,
                    body={},
                )
            )
    if PIPELINE_SHEET in workbook:
        try:
            format_pipeline_service(service, spreadsheet_id, workbook[PIPELINE_SHEET])
        except Exception:  # noqa: BLE001 - formatting must never abort the sync
            print("!!! Pipeline formatting FAILED (sync values are fine) - detail below:")
            traceback.print_exc()


def service_sheet_values(service: Any, spreadsheet_id: str, sheet_name: str) -> list[list[Any]]:
    escaped = sheet_name.replace("'", "''")
    try:
        response = execute_google_request(
            service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=f"'{escaped}'!A1:ZZ10000",
            )
        )
    except Exception as exc:
        response_obj = getattr(exc, "resp", None) or getattr(exc, "response", None)
        status = getattr(response_obj, "status", None) or getattr(response_obj, "status_code", None)
        if status == 400:
            return []
        raise
    return response.get("values", [])


def merge_existing_manual_values_service(
    service: Any,
    spreadsheet_id: str,
    workbook: dict[str, list[list[Any]]],
) -> None:
    if PIPELINE_SHEET in workbook:
        workbook[PIPELINE_SHEET] = merge_pipeline_values(
            workbook[PIPELINE_SHEET],
            service_sheet_values(service, spreadsheet_id, PIPELINE_SHEET),
            service_sheet_values(service, spreadsheet_id, APPLICATIONS_SHEET),
        )
    elif APPLICATIONS_SHEET in workbook:
        existing_values = service_sheet_values(service, spreadsheet_id, APPLICATIONS_SHEET)
        workbook[APPLICATIONS_SHEET] = merge_application_values(
            workbook[APPLICATIONS_SHEET],
            existing_values,
        )


def sheets_api_call(func: Any, *args: Any, max_tries: int = 5, **kwargs: Any) -> Any:
    retryable_statuses = {429, 500, 502, 503}
    for attempt in range(max_tries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            response = getattr(exc, "resp", None) or getattr(exc, "response", None)
            status = getattr(response, "status", None) or getattr(response, "status_code", None)
            if status not in retryable_statuses or attempt == max_tries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def ordered_sheet_names(workbook: dict[str, list[list[Any]]]) -> list[str]:
    preferred = [
        PIPELINE_SHEET,
        SUMMARY_SHEET,
        SETTINGS_SHEET,
        SOURCES_SHEET,
    ]
    return [name for name in preferred if name in workbook] + [
        name for name in workbook if name not in preferred
    ]


def column_letter(column_number: int) -> str:
    if column_number < 1:
        raise ValueError("column_number must be positive")
    result = ""
    while column_number:
        column_number, remainder = divmod(column_number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def open_google_spreadsheet(
    client: Any,
    *,
    spreadsheet_id: str | None,
    sheet_name: str | None,
    create_if_missing: bool = False,
) -> Any:
    try:
        if spreadsheet_id:
            return sheets_api_call(client.open_by_key, spreadsheet_id)
        if not sheet_name:
            raise SystemExit("Missing Google Sheet name. Set GOOGLE_SHEET_NAME or pass --sheet-name.")
        return sheets_api_call(client.open, sheet_name)
    except Exception as exc:
        if exc.__class__.__name__ == "SpreadsheetNotFound":
            if create_if_missing and sheet_name:
                return sheets_api_call(client.create, sheet_name)
            target = spreadsheet_id or sheet_name
            raise SystemExit(f"Google Sheet not found: {target}") from exc
        raise


def worksheet_by_title(spreadsheet: Any, sheet_name: str, values: list[list[Any]]) -> Any:
    try:
        return sheets_api_call(spreadsheet.worksheet, sheet_name)
    except Exception as exc:
        if exc.__class__.__name__ != "WorksheetNotFound":
            raise
    row_count = max(len(values) + 100, 100)
    col_count = max(len(values[0]) if values else 1, 1)
    return sheets_api_call(
        spreadsheet.add_worksheet,
        title=sheet_name,
        rows=row_count,
        cols=col_count,
    )


def positive_int_attr(obj: Any, attr_name: str, default: int) -> int:
    value = getattr(obj, attr_name, default)
    if isinstance(value, int) and value > 0:
        return value
    return default


def ensure_worksheet_size(worksheet: Any, values: list[list[Any]]) -> None:
    width = max(len(values[0]) if values else 1, 1)
    height = max(len(values) + 100, 100)
    current_rows = positive_int_attr(worksheet, "row_count", height)
    current_cols = positive_int_attr(worksheet, "col_count", width)
    target_rows = max(current_rows, height)
    target_cols = max(current_cols, width)
    if target_rows != current_rows or target_cols != current_cols:
        sheets_api_call(worksheet.resize, rows=target_rows, cols=target_cols)


def worksheet_values(spreadsheet: Any, sheet_name: str) -> list[list[Any]]:
    try:
        worksheet = sheets_api_call(spreadsheet.worksheet, sheet_name)
    except Exception as exc:
        if exc.__class__.__name__ == "WorksheetNotFound":
            return []
        raise
    return sheets_api_call(worksheet.get_all_values)


def merge_existing_manual_values_gspread(
    spreadsheet: Any,
    workbook: dict[str, list[list[Any]]],
) -> None:
    if PIPELINE_SHEET in workbook:
        workbook[PIPELINE_SHEET] = merge_pipeline_values(
            workbook[PIPELINE_SHEET],
            worksheet_values(spreadsheet, PIPELINE_SHEET),
            worksheet_values(spreadsheet, APPLICATIONS_SHEET),
        )
    elif APPLICATIONS_SHEET in workbook:
        existing_values = worksheet_values(spreadsheet, APPLICATIONS_SHEET)
        workbook[APPLICATIONS_SHEET] = merge_application_values(
            workbook[APPLICATIONS_SHEET],
            existing_values,
        )


def sync_values_gspread(spreadsheet: Any, workbook: dict[str, list[list[Any]]]) -> None:
    for sheet_name in ordered_sheet_names(workbook):
        values = workbook[sheet_name]
        worksheet = worksheet_by_title(spreadsheet, sheet_name, values)
        ensure_worksheet_size(worksheet, values)
        sheets_api_call(
            worksheet.update,
            range_name="A1",
            values=values,
            value_input_option="RAW",
        )
        clear_start_row = len(values) + 1
        width = max(len(values[0]) if values else 1, 1)
        grid_cols = max(width, positive_int_attr(worksheet, "col_count", width))
        clear_end_col = column_letter(grid_cols)
        clear_ranges = [f"A{clear_start_row}:{clear_end_col}10000"]
        if values and width < grid_cols:
            clear_ranges.append(f"{column_letter(width + 1)}1:{clear_end_col}{len(values)}")
        sheets_api_call(
            worksheet.batch_clear,
            clear_ranges,
        )
    cleanup_worksheets_gspread(spreadsheet, workbook)
    if PIPELINE_SHEET in workbook:
        try:
            format_pipeline_gspread(spreadsheet, workbook[PIPELINE_SHEET])
        except Exception:  # noqa: BLE001 - formatting must never abort the sync
            print("!!! Pipeline formatting FAILED (sync values are fine) - detail below:")
            traceback.print_exc()


def sheet_values_are_blank(values: list[list[Any]]) -> bool:
    return not any(str(cell).strip() for row in values for cell in row)


def cleanup_worksheets_gspread(spreadsheet: Any, workbook: dict[str, list[list[Any]]]) -> None:
    """Remove blank default tabs and put generated tabs in the expected order."""
    worksheets = sheets_api_call(spreadsheet.worksheets)
    generated_titles = set(workbook)
    for worksheet in list(worksheets):
        if worksheet.title not in DEFAULT_BLANK_SHEET_TITLES or worksheet.title in generated_titles:
            continue
        if len(worksheets) <= 1:
            continue
        values = sheets_api_call(worksheet.get_all_values)
        if not sheet_values_are_blank(values):
            continue
        sheets_api_call(spreadsheet.del_worksheet, worksheet)
        worksheets.remove(worksheet)

    if PIPELINE_SHEET in workbook:
        for worksheet in list(worksheets):
            if worksheet.title not in LEGACY_GENERATED_SHEETS:
                continue
            if len(worksheets) <= 1:
                continue
            sheets_api_call(spreadsheet.del_worksheet, worksheet)
            worksheets.remove(worksheet)

    desired_order = ordered_sheet_names(workbook)
    by_title = {worksheet.title: worksheet for worksheet in worksheets}
    ordered = [by_title[title] for title in desired_order if title in by_title]
    ordered.extend(worksheet for worksheet in worksheets if worksheet.title not in desired_order)
    if ordered and [worksheet.title for worksheet in ordered] != [worksheet.title for worksheet in worksheets]:
        sheets_api_call(spreadsheet.reorder_worksheets, ordered)


def env_or_arg(value: str | None, env_name: str) -> str | None:
    return value or os.getenv(env_name)


def first_value(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def resolve_path(value: str | Path | None, default: Path | None = None) -> Path | None:
    raw = value if value is not None else default
    if raw is None:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def require_repo_local_path(path: Path, label: str) -> None:
    try:
        path.relative_to(BASE_DIR)
    except ValueError as exc:
        raise SystemExit(
            f"{label} must be inside this repository: {path}. "
            "Use GOOGLE_PARENT_TOKEN_PATH for a read-only parent token seed."
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync apartment tracker data to Google Sheets.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCE_REGISTRY)
    parser.add_argument(
        "--sheet-name",
        help=f"Google Sheet name. Defaults to GOOGLE_SHEET_NAME or {DEFAULT_SHEET_NAME!r}.",
    )
    parser.add_argument("--spreadsheet-id", help="Optional Google Sheet ID. Defaults to GOOGLE_SHEET_ID.")
    parser.add_argument(
        "--credentials",
        type=Path,
        help=(
            "OAuth client credentials JSON path. Defaults to GOOGLE_CREDENTIALS_PATH "
            f"or {DEFAULT_OAUTH_CREDENTIALS_PATH}."
        ),
    )
    parser.add_argument(
        "--token",
        type=Path,
        help=(
            "OAuth token JSON path. Defaults to GOOGLE_TOKEN_PATH or repo-local token.json. "
            "If missing, the parent repo token.json is used as a read-only seed."
        ),
    )
    parser.add_argument(
        "--service-account-credentials",
        type=Path,
        help="Optional legacy service account JSON path. Defaults to GOOGLE_SHEETS_CREDENTIALS_PATH.",
    )
    parser.add_argument(
        "--create-if-missing",
        action="store_true",
        help="Create the Google Sheet when opening by name fails. Ignored when --spreadsheet-id is used.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build sheet values and print counts without uploading.")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(BASE_DIR / ".env")
    args = build_parser().parse_args(argv)

    with closing(open_db(args.db)) as conn:
        workbook = build_workbook_values(conn, args.sources)

    if args.dry_run:
        for sheet_name, values in workbook.items():
            print(f"{sheet_name}: {max(len(values) - 1, 0)} data rows")
        return 0

    spreadsheet_id = env_or_arg(args.spreadsheet_id, "GOOGLE_SHEET_ID")
    sheet_name = first_value(args.sheet_name, os.getenv("GOOGLE_SHEET_NAME"), DEFAULT_SHEET_NAME)
    credentials = resolve_path(
        first_value(str(args.credentials) if args.credentials else None, os.getenv("GOOGLE_CREDENTIALS_PATH")),
        DEFAULT_OAUTH_CREDENTIALS_PATH,
    )
    raw_token_path = first_value(str(args.token) if args.token else None, os.getenv("GOOGLE_TOKEN_PATH"))
    token = resolve_path(raw_token_path, DEFAULT_OAUTH_TOKEN_PATH)
    parent_token = resolve_path(os.getenv("GOOGLE_PARENT_TOKEN_PATH"), DEFAULT_PARENT_OAUTH_TOKEN_PATH)
    if token:
        try:
            require_repo_local_path(token, "Google OAuth token path")
        except SystemExit:
            if args.token:
                raise
            parent_token = token
            token = DEFAULT_OAUTH_TOKEN_PATH.resolve()
    service_account_credentials = resolve_path(
        first_value(
            str(args.service_account_credentials) if args.service_account_credentials else None,
            os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH"),
        )
    )
    service_account_requested = bool(
        args.service_account_credentials or os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH")
    )
    if service_account_requested and not spreadsheet_id:
        raise SystemExit("Service-account sync requires --spreadsheet-id or GOOGLE_SHEET_ID.")

    if service_account_requested and service_account_credentials and spreadsheet_id:
        service = load_google_service(service_account_credentials)
        merge_existing_manual_values_service(service, spreadsheet_id, workbook)
        sync_values(service, spreadsheet_id, workbook)
        print(f"Synced tracker to Google Sheet {spreadsheet_id}: {', '.join(ordered_sheet_names(workbook))}")
        return 0

    if credentials and credentials.exists():
        client = load_google_client(
            credentials,
            token or DEFAULT_OAUTH_TOKEN_PATH,
            source_token_path=parent_token,
        )
        spreadsheet = open_google_spreadsheet(
            client,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            create_if_missing=args.create_if_missing,
        )
        merge_existing_manual_values_gspread(spreadsheet, workbook)
        sync_values_gspread(spreadsheet, workbook)
        target = getattr(spreadsheet, "url", None) or sheet_name or spreadsheet_id
        print(f"Synced tracker to Google Sheet {target}: {', '.join(ordered_sheet_names(workbook))}")
        return 0

    if service_account_credentials and spreadsheet_id:
        service = load_google_service(service_account_credentials)
        merge_existing_manual_values_service(service, spreadsheet_id, workbook)
        sync_values(service, spreadsheet_id, workbook)
        print(f"Synced tracker to Google Sheet {spreadsheet_id}: {', '.join(ordered_sheet_names(workbook))}")
        return 0

    raise SystemExit(
        "Missing Google OAuth credentials. Reuse the parent repo credentials.json/token.json "
        "or pass --service-account-credentials with --spreadsheet-id for the legacy flow."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
