"""
Stage 3: Write scored jobs to Google Sheet.
Input:  .tmp/scored_jobs.json
Output: Google Sheet (deliverable)

Usage:
    python execution/write_jobs_to_sheet.py
    python execution/write_jobs_to_sheet.py --sheet-name "My Job Search"

Prerequisites:
    - credentials.json in project root (Google OAuth Desktop app)
    - First run opens browser for OAuth consent -> generates token.json
    See directives/setup_google_auth.md for setup instructions.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from gspread_formatting import (
    BooleanCondition,
    BooleanRule,
    CellFormat,
    Color,
    ConditionalFormatRule,
    GridRange,
    get_conditional_format_rules,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
INPUT_FILE = TMP_DIR / "scored_jobs.json"
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Score",
    "Job_ID",
    "Title",
    "Company",
    "Location",
    "Source",
    "Key Matches",
    "Key Gaps",
    "Degree Required",
    "Languages OK",
    "Reasoning",
    "Description",
    "URL",
    "Date Posted",
    "Date Scraped",
    # Application tracking columns
    "Status",
    "Date_Applied",
    "Application_Method",
    "Contact_Person",
    "Contact_Email",
    "Follow_Up_Sent",
    "Follow_Up_Date",
    "Response_Date",
    "Interview_Date",
    "Notes",
    # Auto-populated by Stage 4 & 5
    "CL_Generated",
    "CL_Quality_Score",
    "CV_Generated",
]

# URL column index (0-based) for deduplication
URL_COL_INDEX = HEADERS.index("URL")


def authenticate() -> gspread.Client:
    """Authenticate with Google Sheets using OAuth 2.0."""
    creds = None

    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            log.warning(f"token.json is corrupted or invalid — deleting and re-authenticating. ({TOKEN_FILE})")
            TOKEN_FILE.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(f"credentials.json not found at {CREDENTIALS_FILE}")
                log.error("See directives/setup_google_auth.md for setup instructions")
                sys.exit(1)
            log.info("Starting OAuth flow (browser will open)...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        log.info("Token saved.")

    return gspread.authorize(creds)


def _normalize_date(date_str: str) -> str:
    """[M9] Normalize dates to Swiss format (DD.MM.YYYY). Returns '' for unparseable."""
    if not date_str or not date_str.strip():
        return ""
    date_str = date_str.strip()
    from datetime import datetime as dt
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.strptime(date_str[:10], fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return date_str  # Return as-is if no format matches


    # Contact extraction consolidated into execution/extract_contacts.py
from execution.extract_contacts import extract_contact_person as _extract_contact
from execution.utils import generate_job_id


def job_to_row(job: dict) -> list:
    """Convert a scored job dict to a spreadsheet row.

    Note: Returns mixed types - score as int, rest as strings.
    This allows NUMBER-based conditional formatting on the score column.
    """
    # Truncate description for sheet (max 500 chars to keep sheet readable)
    desc = str(job.get("description", "") or "")
    if desc == "nan":
        desc = ""
    if len(desc) > 500:
        desc = desc[:497] + "..."

    # Extract contact person for tracking (regex only, no LLM — for sheet speed)
    contact_person = _extract_contact(job.get("description", "")) or ""

    # Compute job_id if not already present (backward compat with old scored_jobs.json)
    job_id = job.get("job_id") or generate_job_id(
        str(job.get("title", "")), str(job.get("company", "")), str(job.get("url", ""))
    )

    return [
        int(job.get("score", 1)),  # Keep as int for NUMBER formatting
        job_id,
        str(job.get("title", "")),
        str(job.get("company", "")),
        str(job.get("location", "")),
        str(job.get("source", "")),
        ", ".join(job.get("key_matches", []) if isinstance(job.get("key_matches"), list) else []),
        ", ".join(job.get("key_gaps", []) if isinstance(job.get("key_gaps"), list) else []),
        "Yes" if job.get("degree_required") else "No",
        "Yes" if job.get("languages_ok", True) else "No",
        str(job.get("reasoning", "")),
        desc,
        str(job.get("url", "")),
        _normalize_date(str(job.get("date_posted", "") or "")),
        str(job.get("scraped_at", "") or ""),
        # Application tracking columns (defaults)
        "New",  # Status
        "",     # Date_Applied (user fills manually)
        "",     # Application_Method (user fills manually)
        contact_person,  # Contact_Person (auto-extracted)
        "",     # Contact_Email (user fills manually for follow-ups)
        "No",   # Follow_Up_Sent
        "",     # Follow_Up_Date
        "",     # Response_Date
        "",     # Interview_Date
        "",     # Notes
        "",     # CL_Generated (auto-populated by Stage 4)
        "",     # CL_Quality_Score (auto-populated by Stage 4)
        "",     # CV_Generated (auto-populated by Stage 5)
    ]


def _sheets_api_call(func, *args, retries=3, **kwargs):
    """Wrapper for Google Sheets API calls with retry on quota errors."""
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 429 and attempt < retries - 1:
                log.warning(f"Sheets API quota hit. Waiting 60s before retry {attempt + 2}/{retries}...")
                time.sleep(60)
                continue
            raise


def update_job_columns(sheet_name: str, job_url: str, updates: dict[str, str]):
    """Update specific columns for a job row identified by URL.

    Args:
        sheet_name: Name of the Google Sheet.
        job_url: The job URL to find the row.
        updates: Dict mapping column header names to new values,
                 e.g. {"CL_Generated": "Yes", "CL_Quality_Score": "4/5"}.
    """
    try:
        client = authenticate()
        spreadsheet = client.open(sheet_name)
        worksheet = spreadsheet.sheet1
    except Exception as e:
        log.warning(f"Sheet update skipped (auth/open failed): {e}")
        return

    if not job_url or not job_url.strip():
        log.debug("Sheet update skipped — no job URL provided")
        return

    existing_data = _sheets_api_call(worksheet.get_all_values)
    if not existing_data or len(existing_data) < 2:
        log.warning("Sheet update skipped — sheet is empty or has no data rows")
        return

    headers = existing_data[0]

    # Find the row by URL
    url_col = None
    for idx, h in enumerate(headers):
        if h == "URL":
            url_col = idx
            break
    if url_col is None:
        log.warning("Sheet update skipped — 'URL' column not found in headers")
        return

    target_row = None
    for row_idx, row in enumerate(existing_data[1:], start=2):  # 1-indexed, skip header
        if len(row) > url_col and row[url_col].strip() == job_url.strip():
            target_row = row_idx
            break

    if target_row is None:
        log.debug(f"Sheet update skipped — URL not found in sheet: {job_url[:60]}...")
        return

    # Build batch of cell updates
    for col_name, value in updates.items():
        col_idx = None
        for idx, h in enumerate(headers):
            if h == col_name:
                col_idx = idx
                break

        if col_idx is None:
            # Column doesn't exist yet in the sheet — need to add header first
            # Extend header row with the missing column
            col_idx = len(headers)
            cell_label = gspread.utils.rowcol_to_a1(1, col_idx + 1)
            _sheets_api_call(worksheet.update_acell, cell_label, col_name)
            headers.append(col_name)
            log.info(f"  Added new column '{col_name}' at {cell_label}")

        cell_label = gspread.utils.rowcol_to_a1(target_row, col_idx + 1)
        _sheets_api_call(worksheet.update_acell, cell_label, str(value))

    log.info(f"  Sheet updated: row {target_row} → {updates}")


def _apply_formatting(worksheet):
    """Apply header formatting and conditional formatting rules."""
    last_col = re.sub(r'\d+$', '', gspread.utils.rowcol_to_a1(1, len(HEADERS)))  # e.g. "AA"
    header_range = f"A1:{last_col}1"
    _sheets_api_call(worksheet.format, header_range, {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
    })
    _sheets_api_call(worksheet.freeze, rows=1)

    # Conditional formatting for score column (A2:A5000)
    rules = get_conditional_format_rules(worksheet)
    rules.clear()

    # Green for scores 8-10
    rules.append(ConditionalFormatRule(
        ranges=[GridRange.from_a1_range("A2:A5000", worksheet)],
        booleanRule=BooleanRule(
            condition=BooleanCondition('NUMBER_GREATER_THAN_EQ', ['8']),
            format=CellFormat(backgroundColor=Color(0.72, 0.88, 0.73)),
        ),
    ))
    # Yellow for scores 5-7
    rules.append(ConditionalFormatRule(
        ranges=[GridRange.from_a1_range("A2:A5000", worksheet)],
        booleanRule=BooleanRule(
            condition=BooleanCondition('CUSTOM_FORMULA', ['=AND(A2>=5, A2<8)']),
            format=CellFormat(backgroundColor=Color(1.0, 0.95, 0.6)),
        ),
    ))
    # Red for scores 1-4
    rules.append(ConditionalFormatRule(
        ranges=[GridRange.from_a1_range("A2:A5000", worksheet)],
        booleanRule=BooleanRule(
            condition=BooleanCondition('NUMBER_LESS', ['5']),
            format=CellFormat(backgroundColor=Color(0.96, 0.7, 0.7)),
        ),
    ))

    # Status column (P) conditional formatting
    status_range = [GridRange.from_a1_range("P2:P5000", worksheet)]

    status_colors = [
        ("New",              Color(0.80, 0.90, 1.00)),  # light blue
        ("Ready_to_Apply",   Color(0.00, 0.80, 0.75)),  # teal
        ("APPLYING",         Color(0.80, 0.92, 0.60)),  # yellow-green
        ("Applied",          Color(0.56, 0.74, 0.96)),  # blue
        ("PAUSED",           Color(1.00, 0.85, 0.40)),  # amber
        ("FAILED",           Color(0.80, 0.40, 0.40)),  # dark red
        ("Follow-Up_Sent",   Color(0.85, 0.75, 0.95)),  # lavender
        ("Interviewing",     Color(0.72, 0.88, 0.73)),  # green
        ("Offer",            Color(0.40, 0.85, 0.50)),  # bright green
        ("Rejected",         Color(0.96, 0.70, 0.70)),  # red
        ("No_Response",      Color(0.87, 0.87, 0.87)),  # light grey
        ("Expired",          Color(0.80, 0.80, 0.80)),  # grey
        ("Duplicate",        Color(1.00, 0.76, 0.38)),  # orange
    ]
    for status_text, color in status_colors:
        rules.append(ConditionalFormatRule(
            ranges=status_range,
            booleanRule=BooleanRule(
                condition=BooleanCondition('TEXT_EQ', [status_text]),
                format=CellFormat(backgroundColor=color),
            ),
        ))

    rules.save()
    log.info("Applied conditional formatting")


def write_to_sheet(client: gspread.Client, jobs: list[dict], sheet_name: str, clear: bool = False):
    """Write scored jobs to a Google Sheet (append-only, never overwrites existing rows)."""
    # Sort new jobs by score descending
    jobs.sort(key=lambda j: j.get("score", 0), reverse=True)

    # Try to open existing sheet, or create new one
    try:
        spreadsheet = client.open(sheet_name)
        log.info(f"Opened existing spreadsheet: '{sheet_name}'")
    except gspread.SpreadsheetNotFound:
        spreadsheet = client.create(sheet_name)
        log.info(f"Created new spreadsheet: '{sheet_name}'")

    worksheet = spreadsheet.sheet1

    # Clear all data rows if requested (keep header + formatting)
    if clear:
        _sheets_api_call(worksheet.batch_clear, ["A2:ZZ"])
        log.info("Cleared all existing data rows")

    # Read existing data to determine what's already in the sheet
    existing_data = _sheets_api_call(worksheet.get_all_values)

    if not existing_data or clear:
        # Empty sheet: write header + all jobs
        rows = [HEADERS] + [job_to_row(j) for j in jobs]
        try:
            _sheets_api_call(worksheet.update, range_name="A1", values=rows)
            log.info(f"Wrote {len(jobs)} jobs + header to new sheet")
        except Exception as e:
            log.error(f"Failed to write data to sheet: {e}")
            raise
    else:
        # Sheet has data — extract existing URLs for deduplication
        existing_urls = set()
        for row in existing_data[1:]:  # Skip header
            if len(row) > URL_COL_INDEX and row[URL_COL_INDEX]:
                existing_urls.add(row[URL_COL_INDEX].strip())

        # Build title+company key set once (for jobs with no URL) — row[2]=Title, row[3]=Company
        existing_title_company_keys = {
            f"{row[2]}|{row[3]}".lower()
            for row in existing_data[1:]
            if len(row) > 3
        }

        # Filter to only new jobs (URL not already in sheet)
        new_jobs = []
        skipped = 0
        for job in jobs:
            job_url = str(job.get("url", "")).strip()
            if job_url and job_url in existing_urls:
                skipped += 1
            elif not job_url:
                # No URL — fall back to title+company dedup
                job_key = f"{job.get('title', '')}|{job.get('company', '')}".lower()
                if job_key in existing_title_company_keys:
                    skipped += 1
                else:
                    new_jobs.append(job)
            else:
                new_jobs.append(job)

        if not new_jobs:
            log.info(f"No new jobs to add (all {skipped} already in sheet)")
            spreadsheet_url = spreadsheet.url
            log.info(f"Spreadsheet URL: {spreadsheet_url}")
            return spreadsheet_url

        # Append new rows after last existing row
        new_rows = [job_to_row(j) for j in new_jobs]
        next_row = len(existing_data) + 1
        required_rows = next_row + len(new_rows) - 1
        if required_rows > worksheet.row_count:
            extra = required_rows - worksheet.row_count + 100  # add 100 buffer
            log.info(f"Expanding sheet by {extra} rows (current: {worksheet.row_count}, needed: {required_rows})")
            _sheets_api_call(worksheet.add_rows, extra)
        try:
            _sheets_api_call(worksheet.update, range_name=f"A{next_row}", values=new_rows)
            log.info(f"Appended {len(new_jobs)} new jobs (skipped {skipped} existing)")
        except Exception as e:
            log.error(f"Failed to append data to sheet: {e}")
            raise

    # Apply formatting (safe to re-apply, idempotent)
    try:
        _apply_formatting(worksheet)
    except Exception as e:
        log.warning(f"Formatting failed (data was written successfully): {e}")

    spreadsheet_url = spreadsheet.url
    log.info(f"Spreadsheet URL: {spreadsheet_url}")
    return spreadsheet_url


def main():
    parser = argparse.ArgumentParser(description="Write scored jobs to Google Sheet")
    parser.add_argument("--sheet-name", default="Swiss Job Search Pipeline", help="Name of the Google Sheet")
    parser.add_argument("--clear", action="store_true", help="Clear all existing rows before writing")
    parser.add_argument("--reformat", action="store_true", help="Re-apply formatting to existing sheet without touching data")
    args = parser.parse_args()

    if args.reformat:
        client = authenticate()
        spreadsheet = client.open(args.sheet_name)
        _apply_formatting(spreadsheet.sheet1)
        log.info(f"Formatting updated: {spreadsheet.url}")
        return spreadsheet.url

    if not INPUT_FILE.exists():
        log.error(f"Input file not found: {INPUT_FILE}")
        log.error("Run execution/evaluate_jobs.py first (Stage 2)")
        sys.exit(1)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    log.info(f"Loaded {len(jobs)} scored jobs from {INPUT_FILE}")

    client = authenticate()
    url = write_to_sheet(client, jobs, args.sheet_name, clear=args.clear)
    log.info(f"Done! Sheet available at: {url}")
    return url


if __name__ == "__main__":
    main()
