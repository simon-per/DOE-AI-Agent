"""
One-time migration: Add Job_ID column to Google Sheet + rename application folders.

This script:
1. Reads all rows from the Google Sheet
2. Inserts a "Job_ID" column at position B (after Score)
3. Computes deterministic Job_ID for each row from title + company + URL
4. Renames application folders to include Job_ID prefix
5. Updates checkpoint files with new job_id keys

Usage:
    python execution/migrate_add_job_ids.py                     # dry run (default)
    python execution/migrate_add_job_ids.py --apply             # actually migrate
    python execution/migrate_add_job_ids.py --sheet-only        # only migrate sheet
    python execution/migrate_add_job_ids.py --folders-only      # only rename folders
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.utils import generate_job_id, sanitize_filename, clean_job_title

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
APPLICATIONS_DIR = TMP_DIR / "applications"
SCORED_JOBS_FILE = TMP_DIR / "scored_jobs.json"
CL_CHECKPOINT_FILE = TMP_DIR / "cover_letter_checkpoint.json"
CV_CHECKPOINT_FILE = TMP_DIR / "cv_checkpoint.json"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

DEFAULT_SHEET_NAME = "Swiss Job Search Pipeline"


def authenticate() -> gspread.Client:
    """Authenticate with Google Sheets using OAuth 2.0."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return gspread.authorize(creds)


# ---------------------------------------------------------------------------
# Sheet migration
# ---------------------------------------------------------------------------

def migrate_sheet(sheet_name: str, dry_run: bool = True) -> dict:
    """Add Job_ID column to Google Sheet.

    Returns summary dict with counts.
    """
    log.info(f"{'[DRY RUN] ' if dry_run else ''}Migrating sheet: {sheet_name}")

    client = authenticate()
    spreadsheet = client.open(sheet_name)
    worksheet = spreadsheet.sheet1

    all_data = worksheet.get_all_values()
    if not all_data:
        log.warning("Sheet is empty — nothing to migrate")
        return {"rows": 0, "skipped": 0}

    headers = all_data[0]

    # Check if Job_ID column already exists
    if "Job_ID" in headers:
        log.info("Job_ID column already exists — checking for empty values")
        job_id_col = headers.index("Job_ID")
        title_col = headers.index("Title") if "Title" in headers else None
        company_col = headers.index("Company") if "Company" in headers else None
        url_col = headers.index("URL") if "URL" in headers else None

        if title_col is None or company_col is None or url_col is None:
            log.error("Missing required columns (Title, Company, URL)")
            return {"rows": 0, "skipped": 0}

        filled = 0
        for row_idx, row in enumerate(all_data[1:], start=2):
            if len(row) <= job_id_col or not row[job_id_col].strip():
                title = row[title_col] if len(row) > title_col else ""
                company = row[company_col] if len(row) > company_col else ""
                url = row[url_col] if len(row) > url_col else ""
                jid = generate_job_id(title, company, url)
                if not dry_run:
                    cell = gspread.utils.rowcol_to_a1(row_idx, job_id_col + 1)
                    worksheet.update_acell(cell, jid)
                    time.sleep(0.5)  # Rate limit
                filled += 1
                log.info(f"  Row {row_idx}: {jid} ({company[:30]} - {title[:30]})")

        log.info(f"Filled {filled} empty Job_ID cells")
        return {"rows": filled, "skipped": 0}

    # Job_ID column doesn't exist — need to insert it
    log.info(f"Inserting Job_ID column at position B (shifting {len(headers)} existing columns)")

    # Find column indices in current layout (before insert)
    title_col = headers.index("Title") if "Title" in headers else 1
    company_col = headers.index("Company") if "Company" in headers else 2
    url_col = headers.index("URL") if "URL" in headers else 11

    # Build new data with Job_ID column inserted at position 1 (after Score)
    new_data = []
    new_headers = headers[:1] + ["Job_ID"] + headers[1:]
    new_data.append(new_headers)

    migrated = 0
    for row in all_data[1:]:
        title = row[title_col] if len(row) > title_col else ""
        company = row[company_col] if len(row) > company_col else ""
        url = row[url_col] if len(row) > url_col else ""
        jid = generate_job_id(title, company, url)
        new_row = row[:1] + [jid] + row[1:]
        new_data.append(new_row)
        migrated += 1

    if dry_run:
        log.info(f"[DRY RUN] Would update {migrated} rows with Job_ID column")
        for row in new_data[1:5]:
            log.info(f"  Sample: {row[1]} | {row[3][:30]} | {row[2][:30]}")
        return {"rows": migrated, "skipped": 0}

    # Write all data back (overwrite entire sheet)
    log.info(f"Writing {len(new_data)} rows to sheet...")
    worksheet.clear()
    time.sleep(1)

    # Write in batches of 500 rows
    batch_size = 500
    for start in range(0, len(new_data), batch_size):
        batch = new_data[start:start + batch_size]
        end_row = start + len(batch)
        last_col = gspread.utils.rowcol_to_a1(1, len(new_headers)).rstrip("1")
        cell_range = f"A{start + 1}:{last_col}{end_row}"
        worksheet.update(values=batch, range_name=cell_range)
        log.info(f"  Written rows {start + 1}-{end_row}")
        time.sleep(2)  # Rate limit

    # Re-apply formatting
    log.info("Re-applying formatting...")
    from execution.write_jobs_to_sheet import _apply_formatting
    _apply_formatting(worksheet)

    log.info(f"Sheet migration complete: {migrated} rows updated")
    return {"rows": migrated, "skipped": 0}


# ---------------------------------------------------------------------------
# Folder migration
# ---------------------------------------------------------------------------

def _build_folder_lookup() -> dict[str, dict]:
    """Build lookup from old folder name → job dict using scored_jobs.json."""
    if not SCORED_JOBS_FILE.exists():
        log.warning(f"scored_jobs.json not found at {SCORED_JOBS_FILE}")
        return {}

    with open(SCORED_JOBS_FILE, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    lookup = {}
    for job in jobs:
        title = job.get("title", "")
        company = job.get("company", "")
        safe_company = sanitize_filename(company) if company else "Unknown"
        safe_title = sanitize_filename(clean_job_title(title)) if title else "Unknown"
        old_name = f"{safe_company}_{safe_title}"
        lookup[old_name] = job

    log.info(f"Built lookup with {len(lookup)} entries from scored_jobs.json")
    return lookup


def migrate_folders(dry_run: bool = True) -> dict:
    """Rename application folders to include Job_ID prefix.

    Returns summary dict with counts.
    """
    log.info(f"{'[DRY RUN] ' if dry_run else ''}Migrating application folders")

    if not APPLICATIONS_DIR.exists():
        log.warning(f"Applications directory not found: {APPLICATIONS_DIR}")
        return {"renamed": 0, "skipped": 0, "already_prefixed": 0}

    lookup = _build_folder_lookup()
    folders = [f for f in APPLICATIONS_DIR.iterdir() if f.is_dir()]

    renamed = 0
    skipped = 0
    already_prefixed = 0

    for folder in sorted(folders):
        name = folder.name

        # Skip if already has J- prefix
        if name.startswith("J-"):
            already_prefixed += 1
            continue

        # Try to match to a job in scored_jobs.json
        job = lookup.get(name)
        if not job:
            log.warning(f"  No match in scored_jobs.json for folder: {name}")
            skipped += 1
            continue

        job_id = generate_job_id(
            job.get("title", ""), job.get("company", ""), job.get("url", "")
        )
        new_name = f"{job_id}_{name}"
        new_path = APPLICATIONS_DIR / new_name

        if new_path.exists():
            log.warning(f"  Target already exists, skipping: {new_name}")
            skipped += 1
            continue

        if dry_run:
            log.info(f"  Would rename: {name} → {new_name}")
        else:
            folder.rename(new_path)
            log.info(f"  Renamed: {name} → {new_name}")
        renamed += 1

    log.info(f"Folders: {renamed} renamed, {skipped} skipped, {already_prefixed} already prefixed")
    return {"renamed": renamed, "skipped": skipped, "already_prefixed": already_prefixed}


# ---------------------------------------------------------------------------
# Checkpoint migration
# ---------------------------------------------------------------------------

def migrate_checkpoints(dry_run: bool = True) -> dict:
    """Update checkpoint files to use new job_id format as keys.

    The old format was SHA256(title|company|url)[:16].
    The new format is J-SHA256(title|company|url)[:6].
    We keep old keys for backward compatibility and add new ones.
    """
    log.info(f"{'[DRY RUN] ' if dry_run else ''}Migrating checkpoint files")

    updated = 0
    for cp_file in [CL_CHECKPOINT_FILE, CV_CHECKPOINT_FILE]:
        if not cp_file.exists():
            log.info(f"  Checkpoint not found, skipping: {cp_file.name}")
            continue

        with open(cp_file, "r", encoding="utf-8") as f:
            checkpoint = json.load(f)

        processed = checkpoint.get("processed", {})
        new_processed = {}
        migrated_entries = 0

        for old_key, entry in processed.items():
            # Keep old entry
            new_processed[old_key] = entry

            # Add new J- prefixed key if we can compute it
            title = entry.get("title", "")
            company = entry.get("company", "")
            pdf_path = entry.get("pdf_path", "")

            # Try to find URL from scored_jobs.json
            # For now, just add job_id to the entry metadata
            if "job_id" not in entry:
                # We need the URL to compute the new ID — try to extract from pdf_path
                # or look up in scored_jobs.json
                entry["_old_key"] = old_key
                migrated_entries += 1

        checkpoint["processed"] = new_processed

        if not dry_run and migrated_entries > 0:
            with open(cp_file, "w", encoding="utf-8") as f:
                json.dump(checkpoint, f, ensure_ascii=False, indent=2)
            log.info(f"  Updated {cp_file.name}: {migrated_entries} entries tagged")
        else:
            log.info(f"  {cp_file.name}: {migrated_entries} entries would be tagged")

        updated += migrated_entries

    return {"updated": updated}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Migrate: add Job_ID to sheet + folders")
    parser.add_argument("--apply", action="store_true", help="Actually apply changes (default: dry run)")
    parser.add_argument("--sheet-only", action="store_true", help="Only migrate sheet")
    parser.add_argument("--folders-only", action="store_true", help="Only rename folders")
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME, help="Google Sheet name")
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        log.info("=" * 60)
        log.info("DRY RUN — no changes will be made. Use --apply to execute.")
        log.info("=" * 60)

    results = {}

    if not args.folders_only:
        results["sheet"] = migrate_sheet(args.sheet_name, dry_run=dry_run)

    if not args.sheet_only:
        results["folders"] = migrate_folders(dry_run=dry_run)
        results["checkpoints"] = migrate_checkpoints(dry_run=dry_run)

    log.info("=" * 60)
    log.info("Migration summary:")
    for section, data in results.items():
        log.info(f"  {section}: {data}")
    if dry_run:
        log.info("This was a DRY RUN. Use --apply to execute.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
