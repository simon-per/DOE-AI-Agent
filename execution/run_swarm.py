"""
Stage 7: Multi-Agent Job Application Swarm — workspace setup + Chrome launcher.

Sets up N isolated Claude Code agent workspaces, each with its own Chrome browser
(via Chrome DevTools MCP), profile clone, and job assignments. Claude Code agents
(your subscription) make all browser decisions — no separate API calls.

Architecture:
    run_swarm.py setup   →  creates .tmp/swarm/agent-N/ workspaces
    run_swarm.py launch  →  starts Chrome instances on ports 9223+
    run_swarm.py status  →  aggregates agent logs
    run_swarm.py kill    →  terminates Chrome instances
    run_swarm.py cleanup →  updates Google Sheet from agent logs

Usage:
    python execution/run_swarm.py setup --agents 3 --limit 9
    python execution/run_swarm.py launch
    python execution/run_swarm.py status
    python execution/run_swarm.py kill
    python execution/run_swarm.py cleanup
    python execution/run_swarm.py cleanup --keep   # don't delete swarm dir
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
APPLICATIONS_DIR = TMP_DIR / "applications"
BROWSER_PROFILE_DIR = TMP_DIR / "browser_profile"
SWARM_DIR = TMP_DIR / "swarm"
SCREENSHOTS_DIR = SWARM_DIR / "screenshots"
TASKS_FILE = SWARM_DIR / "tasks.json"

DEFAULT_SHEET_NAME = "Swiss Job Search Pipeline"
DEFAULT_LIMIT = 20
MAX_LIMIT = 20  # Session cap — restart swarm for more
MAX_AGENTS = 5
MAX_JOBS_PER_AGENT = 5
MAX_LINKEDIN_PER_AGENT = 3  # LinkedIn has aggressive bot detection — fewer per session
BASE_CDP_PORT = 9223

CHROME_PATH = os.getenv("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")

from gspread.utils import rowcol_to_a1
from execution.write_jobs_to_sheet import authenticate
from execution.profile_loader import load_profile

# ---------------------------------------------------------------------------
# Platform concurrency limits
# ---------------------------------------------------------------------------

PLATFORM_CONCURRENCY = {
    "linkedin.com": 1,
    "indeed.com": 2,
    "workday.com": 1,
    "greenhouse.io": 3,
    "lever.co": 3,
    "jobs.ch": 3,
    "glassdoor.com": 2,
    "smartrecruiters.com": 3,
    "_default": 3,
}


def _on_rmtree_error(func, path, exc_info):
    """Handle locked files during rmtree on Windows."""
    log.warning(f"  Could not delete {path} — file may be locked")


def _extract_platform(url: str) -> str:
    """Extract platform domain from a job URL."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "_unknown"
    # Normalize: strip www., match against known platforms
    host = host.lower().removeprefix("www.")
    for platform in PLATFORM_CONCURRENCY:
        if platform != "_default" and (host == platform or host.endswith("." + platform)):
            return platform
    return host or "_unknown"


# ---------------------------------------------------------------------------
# Column mapping (same pattern as send_followups.py)
# ---------------------------------------------------------------------------

_DEFAULT_COL_INDICES = {
    "Score": 0,
    "Job_ID": 1,
    "Title": 2,
    "Company": 3,
    "Location": 4,
    "Description": 11,
    "URL": 12,
    "Date_Scraped": 14,
    "Status": 15,
    "Date_Applied": 16,
    "Application_Method": 17,
    "Notes": 24,
}

_col_indices: dict[str, int] = {}


def _build_column_map(header_row: list[str]) -> dict[str, int]:
    """Build column name -> 0-based index map from the header row.

    Normalizes spaces to underscores so "Date Scraped" (header) matches
    "Date_Scraped" (key).
    """
    global _col_indices
    header_lower = {h.strip().lower().replace(" ", "_"): i for i, h in enumerate(header_row)}
    col_map = {}
    for name, default_idx in _DEFAULT_COL_INDICES.items():
        key = name.lower()
        if key in header_lower:
            col_map[name] = header_lower[key]
        else:
            log.warning(f"  Column '{name}' not found in header, using default index {default_idx}")
            col_map[name] = default_idx
    _col_indices = col_map
    return col_map


def _col(name: str) -> int:
    if _col_indices:
        return _col_indices.get(name, _DEFAULT_COL_INDICES.get(name, 0))
    return _DEFAULT_COL_INDICES.get(name, 0)


def _safe_cell(row: list, col_name: str) -> str:
    idx = _col(col_name)
    if idx >= len(row):
        return ""
    return row[idx].strip()


# ---------------------------------------------------------------------------
# Sheet reading — Freshness > Rating
# ---------------------------------------------------------------------------

def read_ready_jobs(sheet_name: str, all_batches: bool = False):
    """Read jobs with Status='Ready_to_Apply', prioritizing newest scrape batch.

    Returns (jobs, worksheet, all_data) so callers can reuse the sheet connection
    and raw data without a second API call.
    """
    client = authenticate()
    spreadsheet = client.open(sheet_name)
    worksheet = spreadsheet.sheet1
    all_data = worksheet.get_all_values()

    if not all_data or len(all_data) < 2:
        log.info("Sheet is empty or has no data rows")
        return [], worksheet, all_data

    _build_column_map(all_data[0])

    jobs = []
    for row_idx, row in enumerate(all_data[1:], start=2):
        status = _safe_cell(row, "Status")
        if status != "Ready_to_Apply":
            continue

        date_scraped_raw = _safe_cell(row, "Date_Scraped")
        scraped_date = None
        if date_scraped_raw:
            try:
                scraped_date = datetime.fromisoformat(date_scraped_raw)
            except ValueError:
                try:
                    scraped_date = datetime.strptime(date_scraped_raw, "%d.%m.%Y").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    log.warning(f"  Row {row_idx}: unparseable Date_Scraped '{date_scraped_raw}'")

        score_raw = _safe_cell(row, "Score")
        try:
            score = int(score_raw)
        except (ValueError, TypeError):
            score = 0

        jobs.append({
            "row_idx": row_idx,
            "job_id": _safe_cell(row, "Job_ID"),
            "title": _safe_cell(row, "Title"),
            "company": _safe_cell(row, "Company"),
            "location": _safe_cell(row, "Location"),
            "url": _safe_cell(row, "URL"),
            "description": _safe_cell(row, "Description"),
            "score": score,
            "date_scraped": scraped_date,
        })

    if not jobs:
        log.info("No jobs with Status='Ready_to_Apply' found")
        return [], worksheet, all_data

    log.info(f"Found {len(jobs)} jobs with Status='Ready_to_Apply'")

    if not all_batches:
        dated_jobs = [j for j in jobs if j["date_scraped"] is not None]
        if dated_jobs:
            newest_date = max(j["date_scraped"] for j in dated_jobs).date()
            batch_jobs = [j for j in jobs if j["date_scraped"] and j["date_scraped"].date() == newest_date]
            log.info(f"Newest batch: {newest_date} ({len(batch_jobs)} jobs)")
            jobs = batch_jobs
        else:
            log.warning("No Date_Scraped values found — processing all Ready_to_Apply jobs")

    jobs.sort(key=lambda j: j["score"], reverse=True)
    return jobs, worksheet, all_data


# ---------------------------------------------------------------------------
# Application folder lookup
# ---------------------------------------------------------------------------

def find_application_folder(job_id: str) -> Path | None:
    """Find the .tmp/applications/ folder for a job by job_id prefix."""
    if not APPLICATIONS_DIR.exists():
        return None
    for folder in APPLICATIONS_DIR.iterdir():
        if folder.is_dir() and folder.name.startswith(job_id):
            return folder
    return None


def find_document(app_folder: Path, doc_type: str) -> Path | None:
    """Find a PDF in the application folder by type."""
    if not app_folder or not app_folder.exists():
        return None
    patterns = {
        "cv": "CV_*.pdf",
        "cv_ats": "CV_*_ATS.pdf",
        "cover_letter": "Cover_Letter_*.pdf",
    }
    pattern = patterns.get(doc_type)
    if not pattern:
        return None
    matches = list(app_folder.glob(pattern))
    if doc_type == "cv":
        matches = [m for m in matches if "_ATS" not in m.name]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Platform-aware job assignment
# ---------------------------------------------------------------------------

def assign_jobs_to_agents(jobs: list[dict], num_agents: int) -> list[list[dict]]:
    """Assign jobs to agents respecting platform concurrency limits.

    Rules:
    - Max MAX_JOBS_PER_AGENT jobs per agent
    - Platforms with concurrency=1 (LinkedIn, Workday): all jobs go to one agent
    - Platforms with concurrency=N: spread across up to N agents
    - Within each agent, order to minimize domain switching
    """
    # Group jobs by platform
    platform_jobs: dict[str, list[dict]] = {}
    for job in jobs:
        platform = _extract_platform(job["url"])
        platform_jobs.setdefault(platform, []).append(job)

    # Cap LinkedIn jobs per session (aggressive bot detection)
    for platform in ("linkedin.com",):
        if platform in platform_jobs and len(platform_jobs[platform]) > MAX_LINKEDIN_PER_AGENT:
            skipped = len(platform_jobs[platform]) - MAX_LINKEDIN_PER_AGENT
            platform_jobs[platform] = platform_jobs[platform][:MAX_LINKEDIN_PER_AGENT]
            log.warning(f"  Capped {platform} to {MAX_LINKEDIN_PER_AGENT} jobs (skipped {skipped})")

    agents: list[list[dict]] = [[] for _ in range(num_agents)]

    # Assign platform groups respecting concurrency limits
    for platform, pjobs in sorted(platform_jobs.items(), key=lambda x: len(x[1]), reverse=True):
        max_concurrent = PLATFORM_CONCURRENCY.get(platform, PLATFORM_CONCURRENCY["_default"])
        max_concurrent = min(max_concurrent, num_agents)

        # Find agents with fewest jobs (that are under the per-agent cap)
        eligible_agents = [
            i for i in range(num_agents)
            if len(agents[i]) < MAX_JOBS_PER_AGENT
        ]
        if not eligible_agents:
            log.warning(f"All agents at capacity ({MAX_JOBS_PER_AGENT}). Skipping {len(pjobs)} {platform} jobs.")
            continue

        # Use at most max_concurrent agents for this platform
        target_agents = sorted(eligible_agents, key=lambda i: len(agents[i]))[:max_concurrent]

        for idx, job in enumerate(pjobs):
            agent_idx = target_agents[idx % len(target_agents)]
            if len(agents[agent_idx]) < MAX_JOBS_PER_AGENT:
                agents[agent_idx].append(job)
            else:
                # Try next eligible agent
                placed = False
                for alt in target_agents:
                    if len(agents[alt]) < MAX_JOBS_PER_AGENT:
                        agents[alt].append(job)
                        placed = True
                        break
                if not placed:
                    log.warning(f"Could not place job {job['job_id']} — all target agents full")

    # Remove empty agent slots
    agents = [a for a in agents if a]
    return agents


# ---------------------------------------------------------------------------
# Profile cloning (selective)
# ---------------------------------------------------------------------------

def clone_browser_profile(src_dir: Path, dst_dir: Path):
    """Selectively clone browser profile — only cookies, logins, storage.

    Avoids copying the full Chrome directory which can trigger session
    invalidation on some platforms.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Files/dirs to clone from the profile root
    root_files = ["Local State"]
    # Files/dirs to clone from Default/
    default_items = [
        "Cookies", "Cookies-journal",
        "Login Data", "Login Data-journal",
        "Local Storage",
        "Session Storage",
        "Preferences",
        "Web Data", "Web Data-journal",
        "Network",  # Chrome 146+ stores cookies in Network/Cookies
    ]

    # Copy root-level files
    for name in root_files:
        src = src_dir / name
        if src.exists():
            try:
                shutil.copy2(str(src), str(dst_dir / name))
            except PermissionError:
                log.warning(f"  Skipped locked file: {src} (close Chrome using this profile first)")

    # Copy Default/ items
    src_default = src_dir / "Default"
    if not src_default.exists():
        log.warning(f"  Source profile has no Default/ — agent will start with empty browser state")
        return
    dst_default = dst_dir / "Default"
    dst_default.mkdir(parents=True, exist_ok=True)

    for name in default_items:
        src = src_default / name
        dst = dst_default / name
        try:
            if src.is_dir():
                shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
            elif src.exists():
                shutil.copy2(str(src), str(dst))
        except PermissionError:
            log.warning(f"  Skipped locked file: {src} (close Chrome using this profile first)")


# ---------------------------------------------------------------------------
# CLAUDE.md generation
# ---------------------------------------------------------------------------

def generate_agent_claude_md(agent_num: int, jobs: list[dict], profile) -> str:
    """Generate the CLAUDE.md content for an agent workspace."""
    p = profile.personal
    e = profile.experience

    # Sort jobs: LinkedIn last (lower risk platforms first, LinkedIn needs extra care)
    jobs = sorted(jobs, key=lambda j: 1 if "linkedin.com" in (j.get("url") or "").lower() else 0)

    # Job table
    job_rows = []
    doc_rows = []
    for i, job in enumerate(jobs, 1):
        job_rows.append(
            f"| {i} | {job['job_id']} | {job['title']} | {job['company']} | {job['url']} |"
        )
        app_folder = find_application_folder(job["job_id"])
        cv_path = find_document(app_folder, "cv") if app_folder else None
        cl_path = find_document(app_folder, "cover_letter") if app_folder else None
        # Forward-slash normalize for MCP tool compatibility on Windows
        cv_display = str(cv_path).replace("\\", "/") if cv_path else "NOT FOUND"
        cl_display = str(cl_path).replace("\\", "/") if cl_path else "NOT FOUND"
        doc_rows.append(
            f"| {job['job_id']} | {cv_display} | {cl_display} |"
        )

    jobs_table = "\n".join(job_rows)
    docs_table = "\n".join(doc_rows)

    # Languages
    lang_lines = []
    for lang in profile.languages:
        lang_lines.append(f"- {lang.code.upper()}: {lang.level}")
    languages_str = "\n".join(lang_lines)

    # Experience bullets
    en_bullets = e.experience_bullets.get("en", [])
    bullets_str = "\n".join(f"  - {b}" for b in en_bullets)

    # Skills
    skills_str = ", ".join(profile.skills[:30])

    # Education
    edu_str = profile.education.education_localized.get("en", {}).get("description", "")

    screenshots_path = str(SCREENSHOTS_DIR).replace("\\", "/")

    return f"""# Job Application Agent {agent_num}

You are a job application agent. Apply to the jobs listed below using the
Chrome DevTools MCP tools. The browser is already open and connected.

## Your Assigned Jobs

| # | Job_ID | Title | Company | URL |
|---|--------|-------|---------|-----|
{jobs_table}

## Document Paths

| Job_ID | CV PDF | Cover Letter PDF |
|--------|--------|-----------------|
{docs_table}

## Candidate Profile (use ONLY this data)

**Personal:**
- Full Name: {p.name}
- First Name: {p.name.split()[0] if p.name else ''}
- Last Name: {' '.join(p.name.split()[1:]) if p.name and len(p.name.split()) > 1 else ''}
- Email: {p.email}
- Phone: {p.phone}
- Date of Birth: {p.dob}
- Nationality: {p.nationality}
- Location: {p.location}
- LinkedIn: {p.linkedin}
- GitHub: {p.github}
- Availability: {p.availability}
- Driver's License: {p.drivers_license}
- Work Permit: {p.work_permit.get('en', '')}

**Experience:**
{e.role} at {e.company} ({e.company_location}), since {e.role_start} ({e.role_period})
{bullets_str}

**Skills:** {skills_str}

**Languages:**
{languages_str}

**Education:** {edu_str}

## Application Instructions

1. Process jobs in the order listed above.
2. For each job:
   a. Use `navigate_page` to go to the job URL.
   b. Use `take_snapshot` to see the page structure (accessibility tree).
   c. **HUMANIZE:** Scroll the page down slowly using `evaluate_script` with `window.scrollBy(0, window.innerHeight)`. Wait 5-15 seconds (simulate reading the job posting). Scroll back up.
   d. Find and click the Apply / Bewerben button. **Wait 1-3 seconds after the click.**
   e. Use `take_snapshot` to see the application form.
   f. Fill all form fields using ONLY the candidate profile data above. **Wait 0.8-2.5 seconds between filling each field.**
   g. For file upload fields: use `upload_file` with the exact path from the Document Paths table above.
   h. **BEFORE clicking Submit/Absenden:** wait 10-25 seconds (review simulation). Then use `take_screenshot` and save to `{screenshots_path}/{{Job_ID}}-before-submit.png`.
   i. **ASK THE HUMAN:** "Ready to submit application for [Title] at [Company]?" Wait for confirmation.
   j. Click Submit. **Wait 1-3 seconds.**
   k. Use `take_screenshot` on the confirmation page and save to `{screenshots_path}/{{Job_ID}}-confirmation.png`.
   l. Append the result to `agent.log`.

3. After each job, append to `agent.log` (in this agent's directory):
   ```
   [YYYY-MM-DDTHH:MM:SS] [DONE] J-abc123 Application submitted successfully
   ```
   or
   ```
   [YYYY-MM-DDTHH:MM:SS] [PAUSED] J-abc123 CAPTCHA detected — stopped
   ```
   or
   ```
   [YYYY-MM-DDTHH:MM:SS] [ERROR] J-abc123 Page returned 404
   ```

## Humanization Rules (CRITICAL — prevents bot detection)

- **Between fields:** wait 0.8-2.5 seconds before filling each input
- **After clicks:** wait 1-3 seconds after clicking any button or link
- **Between pages:** wait 3-7 seconds after any page navigation
- **Before Apply:** scroll page down once, wait 5-15 seconds, scroll back up
- **Before Submit:** wait 10-25 seconds (review simulation)
- **Scrolling:** use `evaluate_script` with `window.scrollBy(0, window.innerHeight)` — never skip this step
- **Maximum {MAX_JOBS_PER_AGENT} jobs per session.** After {MAX_JOBS_PER_AGENT} jobs, STOP and report [DONE] in agent.log.

## LinkedIn-Specific Rules (CRITICAL)

LinkedIn has the most aggressive bot detection. Follow these rules exactly:

- **Max {MAX_LINKEDIN_PER_AGENT} LinkedIn applications per session.** After {MAX_LINKEDIN_PER_AGENT}, STOP LinkedIn jobs and move to other platforms.
- **After each LinkedIn application:** close the LinkedIn tab using `close_page`, wait 60-120 seconds (cool-down), then open a fresh tab for the next one.
- **Between LinkedIn applications:** navigate to a non-LinkedIn page (e.g., google.com) for 30-60 seconds before opening the next LinkedIn job URL.
- **Never have multiple LinkedIn tabs open** at the same time.
- **If LinkedIn shows "verify your identity" or any security challenge:** immediately log [PAUSED] and stop ALL LinkedIn jobs. Continue with non-LinkedIn jobs only.

## Anti-Hallucination Rules

- ONLY use data from the Candidate Profile section above
- NEVER invent phone numbers, addresses, work history, or skills
- Salary expectations: leave blank or enter "negotiable" / "verhandelbar"
- "How did you hear about us": "Online job search" / "Online Jobsuche"
- Start date / availability: {p.availability}
- Unknown/optional fields: leave blank or select "Prefer not to say" / "Keine Angabe"

## When Unsure — Ask the Human (HITL)

You are running in an interactive Claude Code session. The human is watching.

If you encounter ANY of the following, ASK before proceeding:
- A form field you don't know how to answer (e.g., "Expected salary CHF")
- A dropdown with unclear options (e.g., "Department" with 50 choices)
- A checkbox where the answer isn't in the profile
- A confirmation page — ALWAYS ask "Ready to submit?" before clicking Submit

How to ask: simply output a message describing the situation and wait for the
human's response. Example:
  "The form asks for 'Desired salary range (CHF)'. The profile doesn't
   specify this. Should I leave blank, enter 'negotiable', or a specific number?"

## When to STOP (report [PAUSED] in agent.log)

- CAPTCHA or bot detection screen
- Login / account creation required
- Chrome becomes unresponsive after 3 retries

## When to report [ERROR] in agent.log

- Page returns 404/500
- Application form is behind a paywall
- Job posting has been removed or expired
"""


def generate_mcp_json(port: int) -> dict:
    """Generate the .mcp.json content for an agent workspace."""
    return {
        "mcpServers": {
            "chrome": {
                "command": "npx",
                "args": [
                    "chrome-devtools-mcp@latest",
                    f"--browser-url=http://127.0.0.1:{port}",
                ],
            }
        }
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_setup(args):
    """Create agent workspaces, assign jobs, generate configs."""
    log.info("=" * 60)
    log.info("Stage 7: Swarm Setup")
    log.info(f"  Agents: {args.agents}")
    log.info(f"  Limit: {args.limit}")
    log.info(f"  Sheet: {args.sheet_name}")
    log.info("=" * 60)

    # Clean previous swarm — check for running Chrome instances first
    if SWARM_DIR.exists():
        if TASKS_FILE.exists():
            try:
                prev_tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
                for i in range(1, prev_tasks.get("agents", 0) + 1):
                    port = BASE_CDP_PORT + i - 1
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                        log.error(f"Chrome agent-{i} still running on port {port}. Run 'kill' first.")
                        return
                    except Exception:
                        pass
            except (json.JSONDecodeError, KeyError):
                pass
        log.info("Removing previous swarm workspace...")
        shutil.rmtree(str(SWARM_DIR), onerror=_on_rmtree_error)

    # Read jobs (returns worksheet + all_data for reuse — avoids double sheet read)
    jobs, worksheet, all_data = read_ready_jobs(args.sheet_name, all_batches=args.all_batches)
    if not jobs:
        log.info("No eligible jobs. Mark jobs as 'Ready_to_Apply' in the sheet.")
        return

    # Apply limit
    jobs = jobs[:args.limit]
    log.info(f"Selected {len(jobs)} jobs (limit={args.limit})")

    # Assign to agents with platform-aware scheduling
    agent_assignments = assign_jobs_to_agents(jobs, args.agents)
    actual_agents = len(agent_assignments)
    total_assigned = sum(len(a) for a in agent_assignments)
    log.info(f"Assigned {total_assigned} jobs to {actual_agents} agents:")
    if total_assigned < len(jobs):
        log.warning(f"  {len(jobs) - total_assigned} jobs could not be assigned — reduce --limit or increase --agents")

    for i, agent_jobs in enumerate(agent_assignments, 1):
        platforms = set(_extract_platform(j["url"]) for j in agent_jobs)
        log.info(f"  Agent {i}: {len(agent_jobs)} jobs ({', '.join(platforms)})")
        for j in agent_jobs:
            log.info(f"    [{j['score']}] {j['title']} at {j['company']} ({j['job_id']})")

    # Load profile
    profile = load_profile()
    log.info(f"Loaded profile: {profile.personal.name}")

    # Check browser profile exists
    if not BROWSER_PROFILE_DIR.exists():
        log.warning(f"Browser profile not found at {BROWSER_PROFILE_DIR}")
        log.warning("Creating empty profile dir. You'll need to log in to job sites after launch.")
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Warn about missing documents (CV/CL not generated yet)
    missing_docs = []
    for job in jobs:
        app_folder = find_application_folder(job["job_id"])
        if not app_folder:
            missing_docs.append(job["job_id"])
        else:
            cv = find_document(app_folder, "cv")
            cl = find_document(app_folder, "cover_letter")
            if not cv or not cl:
                missing_docs.append(job["job_id"])
    if missing_docs:
        log.warning(f"  {len(missing_docs)} jobs missing CV or cover letter: {', '.join(missing_docs[:10])}")
        log.warning("  Run Stages 4+5 first, or agents will apply without documents.")

    # Create directories
    SWARM_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    # Create agent workspaces
    for i, agent_jobs in enumerate(agent_assignments, 1):
        agent_dir = SWARM_DIR / f"agent-{i}"
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Clone browser profile (selective)
        profile_dir = agent_dir / "profile"
        clone_browser_profile(BROWSER_PROFILE_DIR, profile_dir)
        log.info(f"  Agent {i}: cloned browser profile")

        # Write .mcp.json
        port = BASE_CDP_PORT + i - 1
        mcp_config = generate_mcp_json(port)
        (agent_dir / ".mcp.json").write_text(
            json.dumps(mcp_config, indent=2), encoding="utf-8"
        )

        # Write CLAUDE.md
        claude_md = generate_agent_claude_md(i, agent_jobs, profile)
        (agent_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")

        # Create empty agent.log
        (agent_dir / "agent.log").write_text("", encoding="utf-8")

    # Write tasks.json (read-only reference for status/cleanup)
    tasks = {
        "created": datetime.now(timezone.utc).isoformat(),
        "agents": actual_agents,
        "sheet_name": args.sheet_name,
        "assignments": {},
    }
    for i, agent_jobs in enumerate(agent_assignments, 1):
        tasks["assignments"][f"agent-{i}"] = [
            {
                "job_id": j["job_id"],
                "title": j["title"],
                "company": j["company"],
                "url": j["url"],
                "score": j["score"],
            }
            for j in agent_jobs
        ]
    TASKS_FILE.write_text(json.dumps(tasks, indent=2), encoding="utf-8")

    # Generate launch_chrome.ps1
    launch_lines = [
        f'$chromePath = "{CHROME_PATH}"',
        "",
    ]
    for i in range(1, actual_agents + 1):
        port = BASE_CDP_PORT + i - 1
        profile_path = str(SWARM_DIR / f"agent-{i}" / "profile").replace("/", "\\")
        launch_lines.append(
            f'Start-Process $chromePath -ArgumentList @('
            f'"--remote-debugging-port={port}", '
            f'"--user-data-dir={profile_path}", '
            f'"--no-first-run", '
            f'"--no-default-browser-check"'
            f')'
        )
        launch_lines.append(f'Write-Host "Chrome agent-{i} launched on port {port}"')
    launch_lines.append(f'Write-Host "All {actual_agents} Chrome instances launched."')
    (SWARM_DIR / "launch_chrome.ps1").write_text("\n".join(launch_lines), encoding="utf-8")

    # Generate kill_chrome.ps1
    kill_lines = []
    for i in range(1, actual_agents + 1):
        port = BASE_CDP_PORT + i - 1
        kill_lines.append(
            f"Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' AND "
            f"CommandLine LIKE '%--remote-debugging-port={port}%'\" | "
            f'ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}'
        )
        kill_lines.append(f'Write-Host "Chrome agent-{i} (port {port}) stopped."')
    (SWARM_DIR / "kill_chrome.ps1").write_text("\n".join(kill_lines), encoding="utf-8")

    # Update sheet: set jobs to APPLYING (reuse worksheet from read_ready_jobs)
    all_urls = set()
    for agent_jobs in agent_assignments:
        for job in agent_jobs:
            if job["url"]:
                all_urls.add(job["url"])

    if all_urls and all_data:
        status_col = _col("Status") + 1  # 1-based for gspread
        batch_updates = []
        for row_idx, row in enumerate(all_data[1:], start=2):
            url = _safe_cell(row, "URL")
            if url in all_urls:
                batch_updates.append({
                    "range": rowcol_to_a1(row_idx, status_col),
                    "values": [["APPLYING"]],
                })
        if batch_updates:
            worksheet.batch_update(batch_updates)
            log.info(f"  Sheet: {len(batch_updates)} jobs set to APPLYING")

    # Print summary
    log.info("")
    log.info("=" * 60)
    log.info("Setup complete!")
    log.info(f"  Workspace: {SWARM_DIR}")
    log.info(f"  Agents: {actual_agents}")
    log.info(f"  Total jobs: {sum(len(a) for a in agent_assignments)}")
    log.info("")
    log.info("Next steps:")
    log.info("  1. Run: python execution/run_swarm.py launch")
    log.info("  2. Verify logins in each Chrome window")
    log.info("  3. Spawn Claude Code sessions (printed after launch)")
    log.info("=" * 60)


def cmd_launch(args):
    """Launch Chrome instances for each agent."""
    if not TASKS_FILE.exists():
        log.error("No swarm setup found. Run 'setup' first.")
        return

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    num_agents = tasks["agents"]

    log.info(f"Launching {num_agents} Chrome instances...")

    # Verify Chrome exists
    if not Path(CHROME_PATH).exists():
        log.error(f"Chrome not found at {CHROME_PATH}")
        log.error("Set CHROME_PATH in the script or install Chrome.")
        return

    # Launch each Chrome
    for i in range(1, num_agents + 1):
        port = BASE_CDP_PORT + i - 1
        profile_dir = SWARM_DIR / f"agent-{i}" / "profile"

        cmd = [
            CHROME_PATH,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info(f"  Chrome agent-{i} launched on port {port}")
        time.sleep(1)

    # Verify connectivity
    log.info("Verifying Chrome connectivity...")
    for i in range(1, num_agents + 1):
        port = BASE_CDP_PORT + i - 1
        url = f"http://127.0.0.1:{port}/json/version"
        for attempt in range(5):
            try:
                urllib.request.urlopen(url, timeout=3)
                log.info(f"  Agent {i} (port {port}): OK")
                break
            except Exception:
                if attempt < 4:
                    time.sleep(2)
                else:
                    log.warning(f"  Agent {i} (port {port}): NOT REACHABLE — Chrome may still be starting")

    # Print spawn instructions
    swarm_abs = str(SWARM_DIR).replace("/", "\\")
    log.info("")
    log.info("=" * 60)
    log.info("Chrome instances running. Now spawn Claude Code sessions:")
    log.info("")
    for i in range(1, num_agents + 1):
        agent_path = f"{swarm_abs}\\agent-{i}"
        log.info(f'  wt -w 0 new-tab -d "{agent_path}" -- claude')
    log.info("")
    log.info("Or all at once:")
    wt_parts = []
    for i in range(1, num_agents + 1):
        agent_path = f"{swarm_abs}\\agent-{i}"
        wt_parts.append(f'new-tab -d "{agent_path}" -- claude')
    log.info(f'  wt -w 0 {" ; ".join(wt_parts)}')
    log.info("")
    log.info("IMPORTANT: Verify logins in each Chrome window before starting agents!")
    log.info("=" * 60)


def cmd_status(args):
    """Aggregate and display agent log statuses."""
    if not SWARM_DIR.exists():
        log.error("No swarm workspace found. Run 'setup' first.")
        return

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8")) if TASKS_FILE.exists() else {}
    num_agents = tasks.get("agents", 0)

    job_status = {}

    for i in range(1, num_agents + 1):
        log_file = SWARM_DIR / f"agent-{i}" / "agent.log"
        if not log_file.exists():
            continue

        log.info(f"\n--- Agent {i} ---")
        content = log_file.read_text(encoding="utf-8").strip()
        if not content:
            log.info("  (no activity yet)")
            continue

        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            log.info(f"  {line}")

            # Parse status tags — track latest state per job
            match = re.search(r'\[(DONE|WORKING|PAUSED|ERROR)\]\s+(J-\w+)', line)
            if match:
                job_status[match.group(2)] = match.group(1)

    # Derive summary from unique job states (not raw line counts)
    counts = Counter(job_status.values())
    log.info("")
    log.info("=" * 40)
    log.info("Summary:")
    log.info(f"  Completed: {counts.get('DONE', 0)}")
    log.info(f"  Working:   {counts.get('WORKING', 0)}")
    log.info(f"  Paused:    {counts.get('PAUSED', 0)}")
    log.info(f"  Errors:    {counts.get('ERROR', 0)}")
    log.info("=" * 40)


def cmd_kill(args):
    """Kill all Chrome instances on swarm ports."""
    if not TASKS_FILE.exists():
        log.error("No swarm setup found.")
        return

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    num_agents = tasks["agents"]

    log.info(f"Killing {num_agents} Chrome instances...")

    # Use PowerShell to find and kill Chrome processes by command line
    for i in range(1, num_agents + 1):
        port = BASE_CDP_PORT + i - 1
        cmd = (
            f'powershell -Command "Get-CimInstance Win32_Process '
            f"-Filter \\\"Name='chrome.exe' AND CommandLine LIKE "
            f"'%--remote-debugging-port={port}%'\\\" | "
            f'ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"'
        )
        subprocess.run(cmd, shell=True, capture_output=True)
        log.info(f"  Agent {i} (port {port}): stopped")

    log.info("All Chrome instances terminated.")


def cmd_cleanup(args):
    """Read agent logs, update Google Sheet, optionally remove swarm dir."""
    if not TASKS_FILE.exists():
        log.error("No swarm setup found.")
        return

    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    sheet_name = tasks["sheet_name"]
    num_agents = tasks["agents"]
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    # Build job_id → url mapping from tasks
    job_urls = {}
    for agent_key, agent_jobs in tasks["assignments"].items():
        for job in agent_jobs:
            job_urls[job["job_id"]] = job["url"]

    # Parse all agent logs for final outcomes
    job_outcomes = {}  # job_id -> (tag, reason)
    for i in range(1, num_agents + 1):
        log_file = SWARM_DIR / f"agent-{i}" / "agent.log"
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8").split("\n"):
            match = re.search(r'\[(DONE|PAUSED|ERROR)\]\s+(J-\w+)\s*(.*)', line)
            if match:
                tag, job_id, reason = match.group(1), match.group(2), match.group(3).strip()
                job_outcomes[job_id] = (tag, reason)

    # Update sheet (batch to avoid rate limits)
    client = authenticate()
    spreadsheet = client.open(sheet_name)
    worksheet = spreadsheet.sheet1
    all_data = worksheet.get_all_values()

    if not all_data:
        log.warning("Sheet is empty — cannot update")
    else:
        _build_column_map(all_data[0])
        status_col = _col("Status") + 1  # 1-based for gspread
        date_applied_col = _col("Date_Applied") + 1
        method_col = _col("Application_Method") + 1
        notes_col = _col("Notes") + 1

        # Build URL → row_idx mapping
        url_to_row = {}
        for row_idx, row in enumerate(all_data[1:], start=2):
            url = _safe_cell(row, "URL")
            if url:
                url_to_row[url] = row_idx

        batch_updates = []
        updated = 0

        # Outcome updates
        for job_id, (outcome, reason) in job_outcomes.items():
            url = job_urls.get(job_id)
            if not url or url not in url_to_row:
                continue
            row = url_to_row[url]

            if outcome == "DONE":
                batch_updates.append({"range": rowcol_to_a1(row, status_col), "values": [["Applied"]]})
                batch_updates.append({"range": rowcol_to_a1(row, date_applied_col), "values": [[now_str]]})
                batch_updates.append({"range": rowcol_to_a1(row, method_col), "values": [["Swarm"]]})
                updated += 1
            elif outcome == "PAUSED":
                batch_updates.append({"range": rowcol_to_a1(row, status_col), "values": [["PAUSED"]]})
                updated += 1
            elif outcome == "ERROR":
                batch_updates.append({"range": rowcol_to_a1(row, status_col), "values": [["FAILED"]]})
                updated += 1

            # Write error/pause reason to Notes column
            if reason and outcome in ("PAUSED", "ERROR"):
                batch_updates.append({"range": rowcol_to_a1(row, notes_col), "values": [[reason[:500]]]})

        # Jobs that were assigned but have no outcome → reset to Ready_to_Apply
        # Only reset if current status is still APPLYING (don't overwrite manual changes)
        all_assigned = set(job_urls.keys())
        completed_ids = set(job_outcomes.keys())
        unfinished = all_assigned - completed_ids
        for job_id in unfinished:
            url = job_urls.get(job_id)
            if url and url in url_to_row:
                row_idx = url_to_row[url]
                current_row = all_data[row_idx - 1]  # all_data is 0-indexed, row_idx is 1-based (header=1)
                current_status = _safe_cell(current_row, "Status")
                if current_status == "APPLYING":
                    batch_updates.append({"range": rowcol_to_a1(row_idx, status_col), "values": [["Ready_to_Apply"]]})

        if batch_updates:
            worksheet.batch_update(batch_updates)

        log.info(f"Sheet updated: {updated} jobs, {len(unfinished)} reset to Ready_to_Apply")

    # Remove swarm dir unless --keep
    if not args.keep:
        # Keep screenshots
        if SCREENSHOTS_DIR.exists() and any(SCREENSHOTS_DIR.iterdir()):
            screenshots_backup = TMP_DIR / "swarm_screenshots"
            if screenshots_backup.exists():
                shutil.rmtree(str(screenshots_backup))
            shutil.move(str(SCREENSHOTS_DIR), str(screenshots_backup))
            log.info(f"Screenshots preserved at {screenshots_backup}")

        shutil.rmtree(str(SWARM_DIR), onerror=_on_rmtree_error)
        log.info("Swarm workspace removed.")
    else:
        log.info("Swarm workspace kept (--keep flag).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Stage 7: Multi-Agent Job Application Swarm")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # setup
    p_setup = subparsers.add_parser("setup", help="Create agent workspaces and assign jobs")
    p_setup.add_argument("--agents", type=int, default=3,
                         help=f"Number of agents (1-{MAX_AGENTS}, default: 3)")
    p_setup.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                         help=f"Max jobs total (default: {DEFAULT_LIMIT}, max: {MAX_LIMIT})")
    p_setup.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    p_setup.add_argument("--all-batches", action="store_true",
                         help="Process all batches, not just newest")

    # launch
    subparsers.add_parser("launch", help="Start Chrome instances")

    # status
    subparsers.add_parser("status", help="Show agent progress")

    # kill
    subparsers.add_parser("kill", help="Kill all Chrome instances")

    # cleanup
    p_cleanup = subparsers.add_parser("cleanup", help="Update sheet and remove workspace")
    p_cleanup.add_argument("--keep", action="store_true",
                           help="Keep swarm workspace (don't delete)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Validate args
    if args.command == "setup":
        args.agents = max(1, min(args.agents, MAX_AGENTS))
        args.limit = max(1, min(args.limit, MAX_LIMIT))

    # Dispatch
    commands = {
        "setup": cmd_setup,
        "launch": cmd_launch,
        "status": cmd_status,
        "kill": cmd_kill,
        "cleanup": cmd_cleanup,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
