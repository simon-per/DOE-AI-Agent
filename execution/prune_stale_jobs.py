"""
Prune stale rows from the Swiss Job Search Pipeline sheet.

Deletion modes:

  Unconditional (default: Expired, Duplicate, Irrelevant):
    Deleted regardless of age or score — terminal states that should never linger.

  Date/score-gated (default: New, Rejected):
    Deleted only when BOTH status matches AND at least one condition fires:
      - max(Date Scraped, Date Posted) older than --days days, OR
      - Score <= --max-score (empty Score is ignored / treated as unknown).
    Rows with neither date parseable AND no low score are kept (safe default).

  Applied age-out (default: 45 days):
    Status=Applied rows are deleted when Date_Applied is older than
    --applied-days and no response/interview marker is present. This clears
    ghosted applications without touching rows that have outcome evidence.

Usage:
    python -m execution.prune_stale_jobs --dry-run
    python -m execution.prune_stale_jobs --yes
    python -m execution.prune_stale_jobs --days 30 --max-score 4 --yes
    python -m execution.prune_stale_jobs --applied-days 45 --dry-run
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
DEFAULT_APPLIED_DAYS = 45
DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y")
YES_VALUES = {"yes", "y", "true", "1", "ja", "j", "x"}


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


def cell_value(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def has_response_marker(
    row: list[str],
    *,
    response_date_idx: int | None,
    interview_date_idx: int | None,
    response_received_idx: int | None,
) -> bool:
    if cell_value(row, response_date_idx):
        return True
    if cell_value(row, interview_date_idx):
        return True
    return cell_value(row, response_received_idx).lower() in YES_VALUES


def stale_applied_decision(
    row: list[str],
    *,
    date_applied_idx: int | None,
    response_date_idx: int | None,
    interview_date_idx: int | None,
    response_received_idx: int | None,
    cutoff: datetime,
) -> tuple[bool, str]:
    """Return whether an Applied row should age out, plus a stable reason."""
    if date_applied_idx is None:
        return False, "missing_date_applied_column"
    if has_response_marker(
        row,
        response_date_idx=response_date_idx,
        interview_date_idx=interview_date_idx,
        response_received_idx=response_received_idx,
    ):
        return False, "has_response_marker"

    date_applied = parse_date(cell_value(row, date_applied_idx))
    if date_applied is None:
        return False, "missing_date_applied"
    if date_applied >= cutoff:
        return False, "within_applied_window"
    return True, "applied_age"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Delete rows older than N days (default: 30)")
    ap.add_argument("--max-score", type=int, default=4,
                    help="Delete stale rows with Score <= this value (default: 4). Empty Score is ignored.")
    ap.add_argument("--statuses", default=DEFAULT_STATUSES,
                    help="Comma-separated date/score-gated statuses (case-insensitive)")
    ap.add_argument("--unconditional-statuses", default=DEFAULT_UNCONDITIONAL_STATUSES,
                    help="Statuses deleted unconditionally regardless of date/score (default: Expired,Duplicate,Irrelevant)")
    ap.add_argument(
        "--applied-days", type=int, default=DEFAULT_APPLIED_DAYS,
        help="Delete Status=Applied rows with Date_Applied older than this many "
             "calendar days when no response/interview marker exists. Use 0 to disable "
             f"(default: {DEFAULT_APPLIED_DAYS}).",
    )
    ap.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    ap.add_argument("--dry-run", action="store_true", help="Log candidates, do not delete")
    ap.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    ap.add_argument(
        "--archive-drive", action="store_true",
        help="After deletion, move pruned rows' Drive folders to _Archive/ and "
             "permanently delete folders that have been in _Archive/ longer than "
             "--archive-purge-days. Cloud-only; safe to omit on local runs.",
    )
    ap.add_argument(
        "--archive-purge-days", type=int, default=14,
        help="Days a folder lingers in _Archive/ before permanent deletion (default: 14).",
    )
    ap.add_argument(
        "--reconcile-orphans", action="store_true",
        help="After deletion+archive, also archive any active Drive J-* folder whose "
             "job_id is no longer in the Sheet. Cloud-only — relies on --archive-drive's "
             "purge step to clean up the orphan archives after 14 days.",
    )
    ap.add_argument(
        "--orphan-max-pct", type=float, default=0.10,
        help="Refuse orphan-archive if the orphan share exceeds this fraction "
             "(default 0.10 = 10%%). Catches Sheet read errors that would mass-archive "
             "everything. Override with --force-reconcile.",
    )
    ap.add_argument(
        "--orphan-grace-hours", type=int, default=12,
        help="Skip orphan-archive on folders modified within last N hours (default 12). "
             "Avoids racing in-flight CL uploads from pipeline_full / manual runs.",
    )
    ap.add_argument(
        "--force-reconcile", action="store_true",
        help="Bypass --orphan-max-pct safeguard. Use only when a large legitimate "
             "cleanup is expected (e.g., after manually deleting many Sheet rows).",
    )
    args = ap.parse_args()
    if args.applied_days < 0:
        log.error("--applied-days must be >= 0.")
        return 2

    stale_statuses = {s.strip().lower() for s in args.statuses.split(",") if s.strip()}
    unconditional_statuses = {s.strip().lower() for s in args.unconditional_statuses.split(",") if s.strip()}
    cutoff = datetime.now() - timedelta(days=args.days)
    applied_cutoff = (
        datetime.combine(
            (datetime.now() - timedelta(days=args.applied_days)).date(),
            datetime.min.time(),
        )
        if args.applied_days > 0
        else None
    )
    log.info(f"Cutoff date: rows with newest date < {cutoff.strftime('%Y-%m-%d')} are candidates")
    log.info(f"Unconditional statuses (always deleted): {sorted(unconditional_statuses)}")
    log.info(f"Date/score-gated statuses: {sorted(stale_statuses)}")
    log.info(f"Low-score threshold: Score <= {args.max_score} (empty Score ignored)")
    if applied_cutoff is not None:
        log.info(
            "Applied age-out: Status=Applied with Date_Applied < "
            f"{applied_cutoff.strftime('%Y-%m-%d')} and no response/interview marker"
        )
    else:
        log.info("Applied age-out disabled (--applied-days 0)")

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
    date_applied_idx = cmap.get("date_applied")
    response_date_idx = cmap.get("response_date")
    interview_date_idx = cmap.get("interview_date")
    response_received_idx = cmap.get("response_received")
    if applied_cutoff is not None:
        required_applied_cols = {
            "Date_Applied": date_applied_idx,
            "Response_Date": response_date_idx,
            "Interview_Date": interview_date_idx,
            "Response_Received": response_received_idx,
        }
        missing_applied_cols = [
            name for name, idx in required_applied_cols.items() if idx is None
        ]
        if missing_applied_cols:
            log.error(
                "Applied age-out disabled: missing required outcome column(s): "
                f"{', '.join(missing_applied_cols)}. Existing prune rules still run."
            )
            applied_cutoff = None

    delete_indices: list[int] = []  # 1-indexed sheet row numbers
    deleted_status_counts: Counter[str] = Counter()
    deleted_job_ids: list[str] = []
    trigger_counts: Counter[str] = Counter()
    applied_skip_counts: Counter[str] = Counter()
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
        elif status == "applied":
            if applied_cutoff is None:
                applied_skip_counts["disabled"] += 1
                continue
            should_delete, reason = stale_applied_decision(
                row,
                date_applied_idx=date_applied_idx,
                response_date_idx=response_date_idx,
                interview_date_idx=interview_date_idx,
                response_received_idx=response_received_idx,
                cutoff=applied_cutoff,
            )
            if not should_delete:
                applied_skip_counts[reason] += 1
                continue
            trigger_counts[reason] += 1
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
            f"score_only={trigger_counts['score_only']}, both={trigger_counts['both']}, "
            f"applied_age={trigger_counts['applied_age']}"
        )
    if applied_skip_counts:
        log.info(f"Applied age-out skipped: {dict(applied_skip_counts)}")
    if deleted_job_ids:
        log.info(f"Job_IDs: {', '.join(deleted_job_ids)}")

    if args.dry_run:
        log.info("[DRY RUN] No changes made.")
        return 0

    if not delete_indices:
        log.info("No Sheet rows to delete.")
        if not args.archive_drive and not args.reconcile_orphans:
            return 0

    if delete_indices and not args.yes:
        try:
            confirm = input(f"Delete {len(delete_indices)} rows? [y/N] ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm != "y":
            log.info("Aborted by user.")
            return 1

    if delete_indices:
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

    deleted_job_id_set = {j for j in deleted_job_ids if j}
    drive_archive_failed = False
    reconcile_failed = False
    reconcile_result: dict | None = None

    # Drive lifecycle (cloud-only): archive the deleted rows' Drive folders,
    # then purge anything in _Archive/ older than --archive-purge-days.
    # Purge failures stay non-fatal. Archive failures are reconciled below when
    # possible; otherwise return non-zero so downstream orphan cleanup does not
    # permanently delete folders that should first enter _Archive/.
    if args.archive_drive:
        try:
            from execution.drive_upload import archive_folders_by_job_id
            archived = archive_folders_by_job_id(list(deleted_job_id_set))
            log.info(f"Drive: archived {archived} folder(s)")
        except Exception as exc:  # noqa: BLE001
            drive_archive_failed = True
            log.warning(f"Drive archive failed; will rely on reconcile fallback: {exc}")

        try:
            from execution.drive_upload import purge_archive_older_than
            purged, kept = purge_archive_older_than(days=args.archive_purge_days)
            log.info(
                f"Drive: purged {purged} folder(s) older than "
                f"{args.archive_purge_days}d from _Archive ({kept} kept)"
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(f"Drive archive purge failed (non-fatal): {exc}")

    # Drive→Sheet reconciliation (Round 4): archive any active J-* folder whose
    # job_id is no longer in the Sheet (manually deleted rows, legacy state).
    # Uses the loaded rows minus just-deleted Job_IDs: this is the post-delete
    # Sheet state without an extra API read. If direct archive missed a deleted
    # row, reconcile can still move its active folder to _Archive/.
    if args.reconcile_orphans:
        try:
            from execution.drive_upload import archive_orphan_drive_folders
            sheet_job_ids: set[str] = set()
            if job_id_idx is not None:
                for row in rows[1:]:
                    if job_id_idx < len(row):
                        jid = row[job_id_idx].strip()
                        if jid:
                            sheet_job_ids.add(jid)
            sheet_job_ids -= deleted_job_id_set
            reconcile_result = archive_orphan_drive_folders(
                sheet_job_ids,
                max_orphan_pct=args.orphan_max_pct,
                grace_hours=args.orphan_grace_hours,
                force=args.force_reconcile,
            )
            log.info(
                f"Drive reconcile: active={reconcile_result['active']} "
                f"orphans={reconcile_result['orphan_candidates']} "
                f"in_grace={reconcile_result['in_grace']} "
                f"archived={reconcile_result['archived']} "
                f"failed={reconcile_result['failed']} "
                f"aborted={reconcile_result['aborted']}"
            )
            # If a safeguard tripped, surface it so cron output capture flags it.
            # Use a stable token the modal_pipeline summary regex can match.
            if reconcile_result["aborted"]:
                log.warning(
                    f"[reconcile] aborted={reconcile_result['aborted']} "
                    f"pct={reconcile_result['threshold_pct']:.1%} — "
                    f"orphan archive skipped, see safeguard threshold or "
                    f"empty-sheet guard. Re-run with --force-reconcile if "
                    f"the situation is known-good."
                )
        except Exception as exc:  # noqa: BLE001 — best-effort
            reconcile_failed = True
            log.warning(f"Drive reconcile failed (non-fatal): {exc}")

    if drive_archive_failed:
        reconcile_clean = (
            reconcile_result is not None
            and not reconcile_failed
            and not reconcile_result.get("aborted")
            and int(reconcile_result.get("failed", 0)) == 0
        )
        if not reconcile_clean:
            log.error(
                "Drive archive failed and reconcile did not complete cleanly; "
                "returning non-zero so downstream orphan cleanup does not "
                "permanently delete folders that should first enter _Archive/."
            )
            return 1

    if delete_indices and args.reconcile_orphans:
        reconcile_clean = (
            reconcile_result is not None
            and not reconcile_failed
            and not reconcile_result.get("aborted")
            and int(reconcile_result.get("failed", 0)) == 0
        )
        if not reconcile_clean:
            log.error(
                "Rows were deleted but Drive reconcile did not complete cleanly; "
                "returning non-zero so downstream orphan cleanup cannot bypass "
                "the _Archive/ retention window."
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
