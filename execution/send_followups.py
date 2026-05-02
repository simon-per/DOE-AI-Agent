"""
Stage 6: Send follow-up emails for jobs marked as "Applied" in Google Sheet.
Reads the sheet, finds eligible rows, generates personalized follow-up emails
via LLM, and optionally sends them via Gmail API.

DRY RUN by default (prints emails to console). Use --send to actually send.

Timing: --days counts BUSINESS DAYS (Mon-Fri only), not calendar days.
Example: Applied on Friday → 3 business days = Wednesday (skips Sat+Sun).

Status guard: If status changes away from "Applied" (e.g. to "Interviewing",
"Rejected", "Expired", "Duplicate") during the waiting window, the follow-up is NOT sent.

Input:  Google Sheet (reads Status, Date_Applied, Contact_Email, etc.)
Output: Emails sent via Gmail + sheet updated (Follow_Up_Sent, Follow_Up_Date)

Usage:
    python execution/send_followups.py                    # DRY RUN (default)
    python execution/send_followups.py --send             # Actually send emails
    python execution/send_followups.py --days 5           # Wait 5 business days (default: 3)
    python execution/send_followups.py --sheet-name "X"   # Custom sheet name

Prerequisites:
    - credentials.json in project root
    - Gmail API enabled in Google Cloud Console
    - First run with --send will open browser for OAuth consent (Gmail scope)
    See directives/send_followups.md for setup instructions.
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import gspread
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
COMPANY_CACHE_FILE = TMP_DIR / "company_cache.json"
FOLLOWUP_CHECKPOINT_FILE = TMP_DIR / "followup_checkpoint.json"  # [H4] Crash-safe checkpoint
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token_gmail.json"  # Separate token for Gmail scope

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.send",
]

# Shared modules
from execution.llm_client import call_llm as _call_llm_shared
from execution.language_detect import detect_language as _detect_language_full

# Column names used in Google Sheet — mapped to indices at runtime
# Hardcoded defaults used only as fallback if header row lookup fails
# 0-based indices matching enumerate() in _build_column_map
# Sheet: Score(0) Job_ID(1) Title(2) Company(3) Location(4) Source(5) KeyMatches(6) KeyGaps(7)
#   DegreeReq(8) LangsOK(9) Reasoning(10) Description(11) URL(12) DatePosted(13)
#   DateScraped(14) Status(15) DateApplied(16) AppMethod(17) ContactPerson(18)
#   ContactEmail(19) FollowUpSent(20) FollowUpDate(21) ResponseDate(22)
_DEFAULT_COL_INDICES = {
    "Job_ID": 1,
    "Title": 2,
    "Company": 3,
    "Location": 4,
    "Description": 11,
    "URL": 12,
    "Status": 15,
    "Date_Applied": 16,
    "Contact_Person": 18,
    "Contact_Email": 19,
    "Follow_Up_Sent": 20,
    "Follow_Up_Date": 21,
    "Response_Date": 22,
}

# Will be populated at runtime from the header row
_col_indices: dict[str, int] = {}

# [M6] Basic email format validation
_EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

# [H2] Legal suffix stripping — imported from shared utils
from execution.utils import strip_legal_suffixes, normalize_company_key


def _build_column_map(header_row: list[str]) -> dict[str, int]:
    """Build column name -> 0-based index map from the header row.

    Falls back to hardcoded defaults for any columns not found in header.
    """
    col_map = {}
    header_lower = {h.strip().lower(): i for i, h in enumerate(header_row)}

    for name, default_idx in _DEFAULT_COL_INDICES.items():
        # Try exact match (case-insensitive)
        if name.lower() in header_lower:
            col_map[name] = header_lower[name.lower()]
        else:
            log.warning(f"  Column '{name}' not found in header, using default index {default_idx}")
            col_map[name] = default_idx

    return col_map


def _col(name: str) -> int:
    """Get 0-based column index by name. Uses runtime map if available, else defaults."""
    if _col_indices:
        return _col_indices.get(name, _DEFAULT_COL_INDICES.get(name, 0))
    return _DEFAULT_COL_INDICES.get(name, 0)


def _safe_cell(row: list, col_name: str) -> str:
    """Safely get a cell value, returning '' if column index out of range."""
    idx = _col(col_name)
    if idx >= len(row):
        return ""
    return row[idx].strip()


def _normalize_cache_key(company_name: str) -> str:
    """Normalize company name for cache lookup — strip legal suffixes (AG, GmbH, etc.).

    Uses shared normalize_company_key() to match web_search.py's normalization.
    """
    return normalize_company_key(company_name)


def _business_days_between(start_date, end_date) -> int:
    """Count business days (Mon-Fri) between two dates, exclusive of start, inclusive of end.

    Example: Friday → Monday = 1 business day (Mon is the only workday).
    """
    if end_date <= start_date:
        return 0
    count = 0
    current = start_date + timedelta(days=1)
    while current <= end_date:
        if current.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        current += timedelta(days=1)
    return count


from execution.profile_loader import load_profile as _load_profile

_profile = _load_profile()
SENDER_EMAIL = _profile.personal.email
SENDER_NAME = _profile.personal.name

FOLLOWUP_PROMPT = """Write a short follow-up email for a job application.

CONTEXT:
- Candidate: """ + SENDER_NAME + """ (""" + _profile.experience.role + """, CRM/SAP/Power BI experience)
- Applied for: {title} at {company}
- Applied on: {date_applied}
- Contact person: {contact_person}
- Company info: {company_context}

RULES:
- Language: {language} (match the job posting language)
- Length: 3-5 sentences MAXIMUM
- Tone: Professional, warm, not pushy
- Reference the specific role and company
- Express continued interest
- Offer to provide additional information
- Swiss style: No "ß" (use "ss"), no generic clichés
- If German: Use "Guten Tag" greeting (not "Sehr geehrte Damen und Herren")
- Do NOT mention specific skills unless directly relevant

BANNED PHRASES:
- "Ich möchte nachfragen..." (too passive)
- "Hiermit möchte ich..." (too formal/cliché)
- "Ich bin sehr interessiert an..." (too generic)

Respond with ONLY valid JSON:
{{"subject": "Brief subject line in {language}", "body": "The email body text"}}"""


# ---------------------------------------------------------------------------
# Checkpoint (crash-safe follow-up tracking) [H4]
# ---------------------------------------------------------------------------

def _load_followup_checkpoint() -> set:
    """Load set of (row_idx, contact_email) keys already sent."""
    if FOLLOWUP_CHECKPOINT_FILE.exists():
        try:
            data = json.load(open(FOLLOWUP_CHECKPOINT_FILE, "r", encoding="utf-8"))
            return {tuple(x) for x in data.get("sent", [])}
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def _save_followup_checkpoint(sent: set):
    """Save checkpoint after each successful send."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(FOLLOWUP_CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent": [list(x) for x in sent], "updated_at": datetime.now().isoformat()}, f)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate():
    """Authenticate with Google Sheets + Gmail using OAuth 2.0."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(f"credentials.json not found at {CREDENTIALS_FILE}")
                log.error("See directives/send_followups.md for setup instructions")
                sys.exit(1)
            log.info("Starting OAuth flow (browser will open)...")
            log.info("You will need to grant Gmail send permission.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        log.info("Token saved.")

    sheets_client = gspread.authorize(creds)
    gmail_service = build("gmail", "v1", credentials=creds)
    return sheets_client, gmail_service, creds


# ---------------------------------------------------------------------------
# LLM calls — delegates to shared llm_client
# ---------------------------------------------------------------------------

def _call_llm(openrouter_key, gemini_key, prompt, temperature: float = 0.3):
    """Call LLM via shared client. Returns response text only."""
    result, _ = _call_llm_shared(openrouter_key, gemini_key, prompt, temperature=temperature, max_tokens=500, json_mode=True)
    return result


def detect_language(title: str, description: str) -> str:
    """Detect job language using shared module. Returns language name string."""
    _, lang_name = _detect_language_full(title, description)
    return lang_name


# ---------------------------------------------------------------------------
# Company cache [C2] — handle rich dict format + [H2] key normalization
# ---------------------------------------------------------------------------

def _load_company_cache() -> dict:
    if COMPANY_CACHE_FILE.exists():
        try:
            with open(COMPANY_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _get_company_context(company_name: str, company_cache: dict) -> str:
    """Look up company description from cache, handling both old (str) and new (dict) formats.

    Uses normalized cache key (strips AG/GmbH/etc) to match web_search.py behavior.
    Falls back to unnormalized key for backward compatibility with pre-normalization entries.
    """
    if not company_name:
        return ""

    # Try normalized key first (new entries use this format)
    normalized_key = _normalize_cache_key(company_name)
    entry = company_cache.get(normalized_key)

    # Fallback: try unnormalized key (old entries before AG/GmbH stripping)
    if entry is None:
        entry = company_cache.get(company_name.strip().lower())

    if entry is None:
        return ""

    # Handle both formats: old = plain string, new = dict with "description" key
    if isinstance(entry, dict):
        return entry.get("description", "")
    return str(entry)


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_gmail(gmail_service, to_email: str, subject: str, body: str):
    """Send an email via Gmail API."""
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to_email
    message["from"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    gmail_service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def find_eligible_jobs(worksheet, min_days: int, max_days: int = 14) -> list[dict]:
    """Find jobs eligible for follow-up from Google Sheet."""
    global _col_indices
    all_data = worksheet.get_all_values()
    if len(all_data) <= 1:
        return []

    # Build column map from header row (resilient to column reordering)
    _col_indices = _build_column_map(all_data[0])
    log.info(f"  Column map built from header: {len(_col_indices)} columns mapped")

    today = datetime.now().date()
    eligible = []

    for row_idx, row in enumerate(all_data[1:], start=2):  # Start at row 2 (1-indexed)
        # [C4] Removed overly aggressive row length check (line 332 in old version).
        # Individual field access below uses _safe_cell() which handles short rows.

        status = _safe_cell(row, "Status")
        date_applied_str = _safe_cell(row, "Date_Applied")
        contact_email = _safe_cell(row, "Contact_Email")
        follow_up_sent = _safe_cell(row, "Follow_Up_Sent")
        response_date = _safe_cell(row, "Response_Date")

        # Must be "Applied" status
        if status.lower() != "applied":
            continue

        # Must NOT have already sent a follow-up
        if follow_up_sent.lower() == "yes":
            continue

        # Must NOT have received a response
        if response_date:
            continue

        # Must have a contact email
        if not contact_email:
            title = _safe_cell(row, "Title") or "?"
            company = _safe_cell(row, "Company") or "?"
            log.info(f"  Skipping '{title}' at {company} — no Contact_Email")
            continue

        # [M6] Validate email format
        if not _EMAIL_REGEX.match(contact_email):
            title = _safe_cell(row, "Title") or "?"
            log.warning(f"  Row {row_idx}: Invalid email format '{contact_email}' for '{title}', skipping")
            continue

        # [M7] Must have Date_Applied and be >= min_days ago
        if not date_applied_str or not date_applied_str.strip():
            continue

        try:
            # Support multiple date formats
            date_applied = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    date_applied = datetime.strptime(date_applied_str.strip(), fmt).date()
                    break
                except ValueError:
                    continue

            if date_applied is None:
                log.warning(f"  Row {row_idx}: Could not parse date '{date_applied_str}'")
                continue

            biz_days = _business_days_between(date_applied, today)
            calendar_days = (today - date_applied).days
            if biz_days < min_days:
                title = _safe_cell(row, "Title") or "?"
                log.info(f"  '{title}' — {biz_days} business days since applied (need {min_days}), skipping")
                continue
            if calendar_days > max_days:
                title = _safe_cell(row, "Title") or "?"
                log.info(f"  '{title}' — applied {calendar_days} calendar days ago (>{max_days}, too old), skipping")
                continue

        except Exception as e:
            log.warning(f"  Row {row_idx}: Date error: {e}")
            continue

        eligible.append({
            "row_idx": row_idx,
            "job_id": _safe_cell(row, "Job_ID"),  # Used as stable checkpoint key
            "title": _safe_cell(row, "Title"),
            "company": _safe_cell(row, "Company"),
            "location": _safe_cell(row, "Location"),
            "description": _safe_cell(row, "Description"),
            "contact_person": _safe_cell(row, "Contact_Person"),
            "contact_email": contact_email,
            "date_applied": date_applied_str,
            "days_since": biz_days,
        })

    return eligible


def _parse_email_response(raw: str, job: dict) -> dict | None:
    """Parse LLM response into {subject, body} dict. Returns None on failure."""
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r'```\s*$', '', cleaned.strip(), flags=re.MULTILINE)

    try:
        result = json.loads(cleaned)
        subject = result.get("subject", "").strip()
        body = result.get("body", "").strip()
        if subject and body:
            return {"subject": subject, "body": body}
    except json.JSONDecodeError:
        pass

    # Heuristic extraction: look for subject/body patterns in raw text
    lines = cleaned.split('\n')
    subject_line = next((l for l in lines if l.lower().startswith('subject:')), None)
    if subject_line:
        subject = subject_line.split(':', 1)[1].strip().strip('"')
        body_lines = [l for l in lines if not l.lower().startswith('subject:')]
        body = '\n'.join(body_lines).strip()
        if body and len(body) >= 50:
            return {"subject": subject, "body": body}

    return None


def generate_followup_email(job: dict, openrouter_key, gemini_key, company_cache: dict, researched_companies: dict) -> dict:
    """Generate a follow-up email using LLM. Retries once on failure. [H3][H7]"""
    language = detect_language(job["title"], job["description"])

    # [C2][H2] Get company context from cache with proper format + key handling
    company_context = _get_company_context(job["company"], company_cache)

    # [M4][M8] On-demand company research if not in cache — deduplicated per company
    if not company_context and job["company"]:
        company_key = job["company"].strip().lower()
        if company_key in researched_companies:
            company_context = researched_companies[company_key]
        else:
            try:
                from execution.web_search import research_company
                result = research_company(
                    company_name=job["company"],
                    location=job.get("location", "Switzerland"),
                    job_description=job.get("description", ""),
                    job_url="",
                )
                if result and isinstance(result, dict):
                    company_context = result.get("description", "")
                    if company_context:
                        log.info(f"  On-demand company research: found context for '{job['company']}'")
            except Exception as e:
                log.warning(f"  On-demand company research failed for '{job['company']}': {e}")
            researched_companies[company_key] = company_context  # cache even if empty

    # Language-aware fallback when no contact person is known
    if job["contact_person"]:
        contact = job["contact_person"]
    elif language == "German":
        contact = "Personalverantwortliche/r"
    elif language == "Italian":
        contact = "Responsabile delle risorse umane"
    else:
        contact = "Hiring Manager"

    prompt = FOLLOWUP_PROMPT.format(
        title=job["title"],
        company=job["company"],
        date_applied=job["date_applied"],
        contact_person=contact,
        company_context=company_context if company_context else "No specific company information available.",
        language=language,
    )

    # [H7] Try up to 2 attempts (retry with higher temperature on parse failure)
    for attempt, temp in enumerate([0.3, 0.5]):
        raw = _call_llm(openrouter_key, gemini_key, prompt, temperature=temp)
        parsed = _parse_email_response(raw, job)
        if parsed:
            subject = parsed["subject"]
            body = parsed["body"]

            # [H3] Quality gate: validate body and subject
            body_word_count = len(body.split())
            if body_word_count < 15:
                log.warning(f"  Body too short ({body_word_count} words), attempt {attempt + 1}")
                continue
            if body_word_count > 200:
                log.warning(f"  Body too long ({body_word_count} words), attempt {attempt + 1}")
                continue
            if len(subject) < 5 or len(subject) > 120:
                log.warning(f"  Subject length invalid ({len(subject)} chars), using fallback")
                subject = f"Follow-up: {job['title']} at {job['company']}"

            return {"subject": subject, "body": body, "language": language}

        log.warning(f"  LLM returned unparseable response (attempt {attempt + 1})")

    # Final fallback: use raw LLM text (stripped of common wrappers)
    log.warning("  All attempts failed, using raw LLM output as body")
    fallback_body = re.sub(r'^(Here\'s|Below is|I\'ve drafted).*?:\s*', '', raw.strip(), flags=re.IGNORECASE)
    return {
        "subject": f"Follow-up: {job['title']} at {job['company']}",
        "body": fallback_body.strip() or raw.strip(),
        "language": language,
    }


def main():
    parser = argparse.ArgumentParser(description="Send follow-up emails for applied jobs")
    parser.add_argument("--send", action="store_true", help="Actually send emails (default: dry run)")
    parser.add_argument("--days", type=int, default=3, help="Minimum days since application (default: 3)")
    parser.add_argument("--max-days", type=int, default=14, help="Maximum days since application (default: 14)")
    parser.add_argument("--sheet-name", default="Swiss Job Search Pipeline", help="Name of the Google Sheet")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Clear follow-up checkpoint and recheck all")
    args = parser.parse_args()

    if not args.send:
        log.info("=" * 60)
        log.info("DRY RUN MODE — No emails will be sent")
        log.info("Use --send flag to actually send emails")
        log.info("=" * 60)

    # [H4] Handle checkpoint reset
    if args.reset_checkpoint and FOLLOWUP_CHECKPOINT_FILE.exists():
        FOLLOWUP_CHECKPOINT_FILE.unlink()
        log.info("Follow-up checkpoint cleared")

    openrouter_key = os.getenv("OPEN_ROUTER_API_KEY")
    gemini_key = os.getenv("GOOGLE_AI_STUDIO_API_KEY")

    if not openrouter_key and not gemini_key:
        log.error("No API keys found. Set OPEN_ROUTER_API_KEY or GOOGLE_AI_STUDIO_API_KEY in .env")
        sys.exit(1)

    # Authenticate
    sheets_client, gmail_service, creds = authenticate()

    # Open sheet
    try:
        spreadsheet = sheets_client.open(args.sheet_name)
    except gspread.SpreadsheetNotFound:
        log.error(f"Spreadsheet '{args.sheet_name}' not found")
        sys.exit(1)

    worksheet = spreadsheet.sheet1

    # Find eligible jobs
    log.info(f"Scanning sheet for jobs applied >= {args.days} business days ago (max {args.max_days} calendar days) with Contact_Email...")
    eligible = find_eligible_jobs(worksheet, min_days=args.days, max_days=args.max_days)

    if not eligible:
        log.info("No jobs eligible for follow-up.")
        return

    log.info(f"Found {len(eligible)} job(s) eligible for follow-up")

    # [H4] Load checkpoint to skip already-sent
    sent_checkpoint = _load_followup_checkpoint()
    if sent_checkpoint:
        log.info(f"  Checkpoint: {len(sent_checkpoint)} follow-ups already sent in previous runs")

    # Load company cache
    company_cache = _load_company_cache()

    # [M4] Pre-cache company research per unique company to avoid redundant API calls
    researched_companies: dict[str, str] = {}

    # Process each eligible job
    sent_count = 0
    skipped_checkpoint = 0
    for i, job in enumerate(eligible):
        # [H1] Use job_id (stable) as checkpoint key — not row_idx (shifts on row insert/delete)
        job_id = job.get("job_id") or f"row_{job['row_idx']}"
        checkpoint_key = (job_id, job["contact_email"])
        if checkpoint_key in sent_checkpoint:
            skipped_checkpoint += 1
            continue

        log.info(f"\n[{i + 1}/{len(eligible)}] {job['title']} at {job['company']}")
        log.info(f"  Applied: {job['date_applied']} ({job['days_since']} business days ago)")
        log.info(f"  Email: {job['contact_email']}")

        # Generate follow-up email
        try:
            email = generate_followup_email(job, openrouter_key, gemini_key, company_cache, researched_companies)
        except Exception as e:
            log.error(f"  Failed to generate email: {e}")
            continue

        # [L4] Enhanced logging with word count
        body_wc = len(email['body'].split())
        log.info(f"  Subject: {email['subject']}")
        log.info(f"  Language: {email['language']}")
        log.info(f"  Body ({body_wc} words):\n    {email['body'][:300]}...")

        if args.send:
            try:
                # STATUS RE-CHECK: Re-read the row right before sending to confirm
                # status is still "Applied". If user changed it during the wait window
                # (e.g. to "Interviewing", "Rejected", "Expired", "Duplicate"), do NOT send the follow-up.
                current_row = worksheet.row_values(job["row_idx"])
                current_status = current_row[_col("Status")].strip() if _col("Status") < len(current_row) else ""
                if current_status.lower() != "applied":
                    log.info(f"  Status changed to '{current_status}' — skipping follow-up")
                    continue

                # [C1] IDEMPOTENT: Update sheet FIRST, then send email.
                # If sheet update fails, we skip sending (better than duplicate emails).
                today = datetime.now().strftime("%d.%m.%Y")
                # Column indices are 1-based in gspread update
                worksheet.update_cell(job["row_idx"], _col("Follow_Up_Sent") + 1, "Yes")
                worksheet.update_cell(job["row_idx"], _col("Follow_Up_Date") + 1, today)
                log.info(f"  Sheet marked: Follow_Up_Sent=Yes, Follow_Up_Date={today}")

                # Now send the email (sheet already marked, so no duplicate on rerun)
                try:
                    send_gmail(gmail_service, job["contact_email"], email["subject"], email["body"])
                    log.info(f"  SENT to {job['contact_email']}")
                    sent_count += 1

                    # [H4] Save checkpoint after successful send
                    sent_checkpoint.add(checkpoint_key)
                    _save_followup_checkpoint(sent_checkpoint)

                except HttpError as e:
                    if e.resp.status == 429:
                        log.error(f"  Gmail quota exceeded — stopping follow-up loop. Resume later.")
                        break  # Stop the loop cleanly; sheet is marked but email not sent
                    else:
                        log.error(f"  Gmail API error (status {e.resp.status}): {e}")
                        # Sheet is marked "Yes" but email failed — log clearly for manual review
                        log.warning(f"  MANUAL ACTION NEEDED: '{job['title']}' at {job['company']} marked as sent but email failed. Check and resend manually.")

            except Exception as e:
                log.error(f"  Failed (sheet update or status re-check): {e}")
                # Email was NOT sent (failure occurred before send_gmail call)
        else:
            log.info("  [DRY RUN] Email NOT sent")

        # Rate limit between sends
        if i < len(eligible) - 1:
            time.sleep(5.0)

    # Summary
    log.info(f"\n{'=' * 60}")
    if skipped_checkpoint:
        log.info(f"Skipped {skipped_checkpoint} already-sent (from checkpoint)")
    if args.send:
        log.info(f"Follow-up complete: {sent_count}/{len(eligible) - skipped_checkpoint} emails sent")
    else:
        log.info(f"DRY RUN complete: {len(eligible) - skipped_checkpoint} emails would be sent")
        log.info("Run with --send to actually send emails")


if __name__ == "__main__":
    main()
