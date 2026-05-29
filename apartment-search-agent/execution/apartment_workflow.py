#!/usr/bin/env python3
"""Run the approved apartment search workflow end to end.

This orchestrates existing deterministic tools:
- Flatfox public API ingestion
- local SQLite scoring/deduping/draft generation
- CSV/Markdown export
- Google Sheets sync

It never sends applications or bypasses portal protections.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DAILY_LIMIT,
    DEFAULT_DB_PATH,
    DEFAULT_DRAFTS_MD,
    DEFAULT_QUEUE_CSV,
    DEFAULT_SITE_DAILY_LIMIT,
    connect,
    export_queue,
    init_db,
    print_queue,
    safe_daily_plan,
)
import imaplib  # noqa: E402

from execution.email_alert_ingest import (  # noqa: E402
    DEFAULT_BATCH_LIMIT as EMAILS_DEFAULT_LIMIT,
    DEFAULT_DAYS as EMAILS_DEFAULT_DAYS,
    DEFAULT_MAILBOX as EMAILS_DEFAULT_MAILBOX,
    IngestStats as EmailIngestStats,
    sync_email_alerts,
)
from execution.flatfox_public_sync import (  # noqa: E402
    DEFAULT_AREA,
    DEFAULT_CORRIDOR_KM,
    DEFAULT_TARGET_CITIES,
    FLATFOX_BASE_URL,
    SyncStats,
    sync_flatfox_public,
)
from execution.google_sheets_sync import (  # noqa: E402
    DEFAULT_SHEET_NAME,
    DEFAULT_SOURCE_REGISTRY,
    main as google_sheets_main,
)


@dataclass(frozen=True)
class WorkflowResult:
    db_path: Path
    dry_run: bool
    flatfox_stats: SyncStats | None
    email_stats: EmailIngestStats | None
    counts: dict[str, int]
    csv_path: Path | None
    drafts_path: Path | None
    google_synced: bool


def tracker_counts(db_path: Path) -> dict[str, int]:
    with closing(connect(db_path)) as conn:
        init_db(conn)
        queries = {
            "total": "SELECT COUNT(*) FROM listings",
            "queue": """
                SELECT COUNT(*) FROM listings
                WHERE decision IN ('apply', 'consider', 'manual_review')
                  AND status NOT IN ('sent', 'archived')
            """,
            "apply": "SELECT COUNT(*) FROM listings WHERE decision = 'apply'",
            "consider": "SELECT COUNT(*) FROM listings WHERE decision = 'consider'",
            "manual_review": "SELECT COUNT(*) FROM listings WHERE decision = 'manual_review'",
            "skip": "SELECT COUNT(*) FROM listings WHERE decision = 'skip'",
            "sent": "SELECT COUNT(*) FROM listings WHERE status = 'sent'",
        }
        return {
            label: int(conn.execute(query).fetchone()[0])
            for label, query in queries.items()
        }


def resolve_repo_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = BASE_DIR / expanded
    return expanded.resolve()


def require_repo_local_path(path: Path, label: str) -> Path:
    resolved = resolve_repo_path(path)
    try:
        resolved.relative_to(BASE_DIR)
    except ValueError as exc:
        raise SystemExit(f"{label} must be inside this repository: {resolved}") from exc
    return resolved


def normalize_workflow_paths(args: argparse.Namespace) -> None:
    args.db = require_repo_local_path(args.db, "SQLite database path")
    args.csv = require_repo_local_path(args.csv, "Queue CSV path")
    args.drafts = require_repo_local_path(args.drafts, "Drafts Markdown path")


@contextmanager
def workflow_paths(args: argparse.Namespace) -> Iterator[tuple[Path, Path, Path]]:
    if not args.dry_run:
        yield args.db, args.csv, args.drafts
        return

    tmp_root = BASE_DIR / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="workflow-dry-run-", dir=tmp_root) as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "listings.sqlite"
        if args.db.exists():
            shutil.copy2(args.db, db_path)
        else:
            with closing(connect(db_path)) as conn:
                init_db(conn)
        yield db_path, tmp_path / "application_queue.csv", tmp_path / "message_drafts.md"


def build_flatfox_args(args: argparse.Namespace, db_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=db_path,
        base_url=args.flatfox_base_url,
        timeout_seconds=args.flatfox_timeout_seconds,
        max_retries=args.flatfox_max_retries,
        limit=args.flatfox_limit,
        offset=args.flatfox_offset,
        max_pages=args.flatfox_max_pages,
        status=args.flatfox_status,
        selection=args.flatfox_selection,
        pk=args.flatfox_pk,
        sleep_seconds=args.flatfox_sleep_seconds,
        city=args.flatfox_city or DEFAULT_TARGET_CITIES,
        area=args.flatfox_area,
        corridor_km=args.flatfox_corridor_km,
        bbox=args.flatfox_bbox,
        max_rent=args.max_rent,
        include_over_budget=args.include_over_budget,
        include_unknown_rent=args.include_unknown_rent,
        verbose=args.verbose,
    )


def build_email_args(args: argparse.Namespace, db_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=db_path,
        mailbox=args.emails_mailbox,
        days=args.emails_days,
        limit=args.emails_limit,
        reprocess=args.emails_reprocess,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


def build_google_args(args: argparse.Namespace, db_path: Path) -> list[str]:
    google_args = [
        "--db",
        str(db_path),
        "--sources",
        str(args.sources),
        "--sheet-name",
        args.sheet_name,
    ]
    if args.spreadsheet_id:
        google_args.extend(["--spreadsheet-id", args.spreadsheet_id])
    if args.create_sheet_if_missing:
        google_args.append("--create-if-missing")
    if args.dry_run:
        google_args.append("--dry-run")
    return google_args


def print_counts(counts: dict[str, int]) -> None:
    print(
        "Tracker counts: "
        f"total={counts['total']}, queue={counts['queue']}, apply={counts['apply']}, "
        f"consider={counts['consider']}, manual_review={counts['manual_review']}, "
        f"skip={counts['skip']}, sent={counts['sent']}"
    )


def run_workflow(args: argparse.Namespace) -> WorkflowResult:
    with workflow_paths(args) as (db_path, csv_path, drafts_path):
        with closing(connect(db_path)) as conn:
            init_db(conn)

        flatfox_stats: SyncStats | None = None
        if args.skip_flatfox:
            print("Flatfox ingestion skipped.")
        else:
            flatfox_stats = sync_flatfox_public(build_flatfox_args(args, db_path))
            print(
                "Flatfox public sync complete: "
                f"pages={flatfox_stats.pages}, fetched={flatfox_stats.fetched}, "
                f"matched={flatfox_stats.matched}, created={flatfox_stats.created}, "
                f"updated={flatfox_stats.updated}, skipped={flatfox_stats.skipped}"
            )

        email_stats: EmailIngestStats | None = None
        if args.skip_emails:
            print("Email alert ingestion skipped.")
        else:
            try:
                email_stats = sync_email_alerts(build_email_args(args, db_path))
                print(
                    "Email alert ingest complete: "
                    f"fetched={email_stats.fetched}, routed={email_stats.routed}, "
                    f"unrouted={email_stats.unrouted}, "
                    f"listings_seen={email_stats.listings_seen}, "
                    f"created={email_stats.listings_created}, "
                    f"updated={email_stats.listings_updated}, "
                    f"skipped={email_stats.listings_skipped}"
                )
            except (SystemExit, imaplib.IMAP4.error, OSError) as exc:
                # Missing/invalid creds or transient IMAP/network errors should
                # surface a clear note but never abort the rest of the workflow.
                print(f"Email alert ingestion unavailable: {exc}")

        if args.skip_export:
            output_csv_path = None
            output_drafts_path = None
            print("Local CSV/Markdown export skipped.")
        else:
            with closing(connect(db_path)) as conn:
                export_queue(conn, csv_path, drafts_path, args.export_limit)
            output_csv_path = None if args.dry_run else csv_path
            output_drafts_path = None if args.dry_run else drafts_path
            print(f"Exported queue and drafts{' in dry-run temp storage' if args.dry_run else ''}.")

        counts = tracker_counts(db_path)
        print_counts(counts)

        if not args.skip_daily_plan:
            with closing(connect(db_path)) as conn:
                plan = safe_daily_plan(conn, args.daily_limit, args.site_daily_limit)
            print("\nSafe daily application plan:")
            print_queue(plan)
            print("No messages were sent. Simon still approves/sends each application.")

        google_synced = False
        if args.skip_google:
            print("Google Sheets sync skipped.")
        else:
            google_sheets_main(build_google_args(args, db_path))
            google_synced = not args.dry_run

        return WorkflowResult(
            db_path=db_path,
            dry_run=args.dry_run,
            flatfox_stats=flatfox_stats,
            email_stats=email_stats,
            counts=counts,
            csv_path=output_csv_path,
            drafts_path=output_drafts_path,
            google_synced=google_synced,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the safe apartment search workflow: ingest, score, export, and sync."
    )
    parser.add_argument("--dry-run", action="store_true", help="Use a temp database/export path and Sheets dry-run.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCE_REGISTRY)
    parser.add_argument("--verbose", action="store_true")

    parser.add_argument("--skip-flatfox", action="store_true", help="Skip documented Flatfox public API ingestion.")
    parser.add_argument("--flatfox-base-url", default=FLATFOX_BASE_URL)
    parser.add_argument("--flatfox-limit", type=int, default=100)
    parser.add_argument("--flatfox-offset", type=int, default=0)
    parser.add_argument("--flatfox-max-pages", type=int, default=3)
    parser.add_argument("--flatfox-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--flatfox-timeout-seconds", type=int, default=30)
    parser.add_argument("--flatfox-max-retries", type=int, default=2)
    parser.add_argument("--flatfox-status", default="act")
    parser.add_argument("--flatfox-selection", type=int)
    parser.add_argument("--flatfox-pk", type=int, action="append")
    parser.add_argument("--flatfox-city", action="append", help="Target city. Repeatable.")
    parser.add_argument("--flatfox-area", default=DEFAULT_AREA, choices=["luzern-rotkreuz", "none"])
    parser.add_argument("--flatfox-corridor-km", type=float, default=DEFAULT_CORRIDOR_KM)
    parser.add_argument("--flatfox-bbox", type=float, nargs=4, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    parser.add_argument("--max-rent", type=int, default=1000)
    parser.add_argument("--include-over-budget", action="store_true")
    parser.add_argument("--include-unknown-rent", action="store_true")

    parser.add_argument("--skip-emails", action="store_true", help="Skip Gmail IMAP saved-search alert ingestion.")
    parser.add_argument("--emails-mailbox", default=EMAILS_DEFAULT_MAILBOX)
    parser.add_argument("--emails-days", type=int, default=EMAILS_DEFAULT_DAYS)
    parser.add_argument("--emails-limit", type=int, default=EMAILS_DEFAULT_LIMIT)
    parser.add_argument("--emails-reprocess", action="store_true", help="Bypass the email_alert_seen dedupe table.")

    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--export-limit", type=int, default=100)
    parser.add_argument("--csv", type=Path, default=DEFAULT_QUEUE_CSV)
    parser.add_argument("--drafts", type=Path, default=DEFAULT_DRAFTS_MD)

    parser.add_argument("--skip-daily-plan", action="store_true")
    parser.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT)
    parser.add_argument("--site-daily-limit", type=int, default=DEFAULT_SITE_DAILY_LIMIT)

    parser.add_argument("--skip-google", action="store_true")
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--create-sheet-if-missing", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    normalize_workflow_paths(args)
    if args.flatfox_limit < 1 or args.flatfox_limit > 100:
        raise SystemExit("--flatfox-limit must be between 1 and 100.")
    if args.flatfox_max_pages < 1:
        raise SystemExit("--flatfox-max-pages must be at least 1.")
    if args.export_limit < 1:
        raise SystemExit("--export-limit must be at least 1.")

    if args.dry_run:
        print("Dry-run mode: permanent tracker, exports, and Google Sheet will not be modified.")

    run_workflow(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
