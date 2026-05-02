"""
Prune stale rows from the Swiss Job Search Pipeline sheet.

Two deletion modes:

  Unconditional (default: Expired, Duplicate, Irrelevant):
    Deleted regardless of age or score — terminal states that should never linger.

  Date/score-gated (default: New, Rejected):
    Deleted only when BOTH status matches AND at least one condition fires:
      - max(Date Scraped, Date Posted) older than --days days, OR
      - Score <= --max-score (empty Score is ignored / treated as unknown).
    Rows with neither date parseable AND no low score are kept (safe default).

Usage:
    python -m execution.prune_stale_jobs --dry-run
    python -m execution.prune_stale_jobs --yes
    python -m execution.prune_stale_jobs --days 30 --max-score 4 --yes
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.write_jobs_to_sheet import authenticate, _sheets_api_call  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("prune_stale_jobs")

DEFAULT_SHEET_NAME = "Swiss Job Search Pipeline"
DEFAULT_STATUSES = "New,Rejected"
DEFAULT_UNCONDITIONAL_STATUSES = "Expired,Duplicate,Irrelevant"
DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y")


def parse_date(value: str) -> datetime | None:
    """Normalize any of the date shapes the sheet has used (or might use).

    Handles:
      - Date Posted today:   '15.04.2026'                       (Swiss DD.MM.YYYY)
      - Date Scraped today:  '2026-04-15T19:53:21.911535+00:00' (ISO 8601 w/ tz)
      - Bare ISO date:       '2026-04-15'
      - Future EU switch:    '15.04.2026' for Date Scraped too
      - Slash variants:      '15/04/2026', '04/15/2026'
    """
    if not value:
        return None
    s = value.strip()
    if not s:
        return None

    # 1. Try full ISO 8601 (handles 'YYYY-MM-DDTHH:MM:SS.ffffff+ZZ:ZZ' and 'Z' suffix).
    iso_attempt = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso_attempt)
        return dt.replace(tzinfo=None)  # strip tz for naive comparison
    except ValueError:
        pass

    # 2. Fall back to date-only formats. Strip any time component first.
    head = s.split("T", 1)[0].split(" ", 1)[0]
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt)
        except ValueError:
            continue
    return None


def parse_score(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def build_column_map(header_row: list[str]) -> dict[str, int]:
    return {h.strip().lower().replace(" ", "_"): i for i, h in enumerate(header_row)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Delete rows older than N days (default: 30)")
    ap.add_argument("--max-score", type=int, default=4,
                    help="Delete stale rows with Score <= this value (default: 4). Empty Score is ignored.")
    ap.add_argument("--statuses", default=DEFAULT_STATUSES,
                    help="Comma-separated date/score-gated statuses (case-insensitive)")
    ap.add_argument("--unconditional-statuses", default=DEFAULT_UNCONDITIONAL_STATUSES,
                    help="Statuses deleted unconditionally regardless of date/score (default: Expired,Duplicate,Irrelevant)")
    ap.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    ap.add_argument("--dry-run", action="store_true", help="Log candidates, do not delete")
    ap.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    args = ap.parse_args()

    stale_statuses = {s.strip().lower() for s in args.statuses.split(",") if s.strip()}
    unconditional_statuses = {s.strip().lower() for s in args.unconditional_statuses.split(",") if s.strip()}
    cutoff = datetime.now() - timedelta(days=args.days)
    log.info(f"Cutoff date: rows with newest date < {cutoff.strftime('%Y-%m-%d')} are candidates")
    log.info(f"Unconditional statuses (always deleted): {sorted(unconditional_statuses)}")
    log.info(f"Date/score-gated statuses: {sorted(stale_statuses)}")
    log.info(f"Low-score threshold: Score <= {args.max_score} (empty Score ignored)")

    client = authenticate()
    spreadsheet = _sheets_api_call(client.open, args.sheet_name)
    worksheet = spreadsheet.sheet1
    sheet_id = worksheet.id

    rows = _sheets_api_call(worksheet.get_all_values)
    if not rows:
        log.info("Sheet is empty; nothing to do.")
        return 0

    header = rows[0]
    cmap = build_column_map(header)
    for required in ("status", "date_scraped", "date_posted", "score"):
        if required not in cmap:
            log.error(f"Required column '{required}' not found in header: {header}")
            return 2

    status_idx = cmap["status"]
    scraped_idx = cmap["date_scraped"]
    posted_idx = cmap["date_posted"]
    score_idx = cmap["score"]
    job_id_idx = cmap.get("job_id")

    delete_indices: list[int] = []  # 1-indexed sheet row numbers
    deleted_status_counts: Counter[str] = Counter()
    deleted_job_ids: list[str] = []
    trigger_counts: Counter[str] = Counter()  # date_only / score_only / both
    kept_stale = 0  # stale-status rows that survived (no rule fired)
    scanned = 0

    for i, row in enumerate(rows[1:], start=2):  # data rows start at sheet row 2
        scanned += 1
        # Tolerate short rows (trailing empty cells trimmed by Sheets)
        def cell(idx: int) -> str:
            return row[idx] if idx < len(row) else ""

        status = cell(status_idx).strip().lower()

        if status in unconditional_statuses:
            trigger_counts["unconditional"] += 1
        elif status in stale_statuses:
            d_scraped = parse_date(cell(scraped_idx))
            d_posted = parse_date(cell(posted_idx))
            candidates = [d for d in (d_scraped, d_posted) if d is not None]
            newest = max(candidates) if candidates else None
            is_old = newest is not None and newest < cutoff

            score = parse_score(cell(score_idx))
            is_low_score = score is not None and score <= args.max_score

            if not (is_old or is_low_score):
                kept_stale += 1
                continue

            if is_old and is_low_score:
                trigger_counts["both"] += 1
            elif is_old:
                trigger_counts["date_only"] += 1
            else:
                trigger_counts["score_only"] += 1
        else:
            continue

        delete_indices.append(i)
        deleted_status_counts[cell(status_idx).strip()] += 1
        if job_id_idx is not None:
            deleted_job_ids.append(cell(job_id_idx))

    log.info(
        f"Scanned {scanned} data rows | candidates to delete: {len(delete_indices)} | "
        f"kept stale (no rule fired): {kept_stale}"
    )
    if deleted_status_counts:
        log.info(f"Breakdown: {dict(deleted_status_counts)}")
    if trigger_counts:
        log.info(
            f"Triggered by: unconditional={trigger_counts['unconditional']}, "
            f"date_only={trigger_counts['date_only']}, "
            f"score_only={trigger_counts['score_only']}, both={trigger_counts['both']}"
        )
    if deleted_job_ids:
        log.info(f"Job_IDs: {', '.join(deleted_job_ids)}")

    if not delete_indices:
        log.info("Nothing to delete.")
        return 0

    if args.dry_run:
        log.info("[DRY RUN] No changes made.")
        return 0

    if not args.yes:
        try:
            confirm = input(f"Delete {len(delete_indices)} rows? [y/N] ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "y":
            log.info("Aborted by user.")
            return 1

    # Delete in descending row order so earlier deletes don't shift later indices.
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": idx - 1,  # API is 0-indexed, end exclusive
                    "endIndex": idx,
                }
            }
        }
        for idx in sorted(delete_indices, reverse=True)
    ]

    _sheets_api_call(spreadsheet.batch_update, {"requests": requests})
    log.info(f"Deleted {len(delete_indices)} row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
