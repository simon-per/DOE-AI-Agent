"""
Cloud-scheduled pipeline runner on Modal (serverless).

Runs all automatable stages 24/7 without the local PC needing to be on:
  - Tue + Thu 17:00 CET: scrape_jobs → evaluate_jobs → write_jobs_to_sheet
  - Every 2 hours:       generate cover letters + CVs for Ready_to_Apply rows
                         → upload to Google Drive → email notification
  - Mon / Wed / Fri 09:00 CET: send_followups --send
  - Daily 05:45 CET: prune_stale_jobs --yes

Stage 7 (run_swarm / apply) stays local — requires Chrome + human review.

Reliability hardening (see .claude/plans/hi-how-are-you-enchanted-tulip.md):
  - Defensive secret validation (clear errors instead of binascii cracks)
  - Per-stage volume.commit() so partial progress survives a mid-run crash
  - Email alert on any stage failure (Gmail via token_gmail.json)
  - max_containers=1 prevents cron + manual triggers overlapping
  - Preflight step verifies all API surfaces before LLM calls

Secrets required (set once in Modal dashboard):
  doe-google-oauth  →  TOKEN_JSON (base64 of token.json, Sheets+Drive scope)
                        TOKEN_GMAIL_JSON (base64 of token_gmail.json, +Gmail.send)
                        CREDENTIALS_JSON (base64 of credentials.json)
  doe-api-keys      →  OPEN_ROUTER_API_KEY, GOOGLE_AI_STUDIO_API_KEY,
                        SERPAPI_API_KEY, SERPAPI_API_KEY2

Volume:
  doe-tmp  →  mounted at /root/workspace/.tmp/ (persists caches between runs)

Images:
  image                — lightweight: scrape / evaluate / sheet / followups / prune
  image_with_playwright — adds Playwright Chromium for CV PDF rendering (Stage 5)

Deploy:   modal deploy execution/modal_pipeline.py
Dry-run:  modal run execution/modal_pipeline.py::preflight
          modal run execution/modal_pipeline.py::pipeline_generate_applications
"""

from __future__ import annotations

import base64
import binascii
import os
import subprocess
import sys
from pathlib import Path

import modal

# ---------------------------------------------------------------------------
# Image — bake in all project files so scripts can import each other
# ---------------------------------------------------------------------------
_base = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        # Needed by python-jobspy and requests for SSL / encoding
        "libssl-dev",
        "ca-certificates",
        "libffi-dev",
    )
    .pip_install_from_requirements("requirements.txt")
    # Bundle project files (scripts, directives, profile)
    .add_local_dir("execution", remote_path="/root/workspace/execution")
    .add_local_dir("directives", remote_path="/root/workspace/directives")
    .add_local_file("ABOUTME.md", remote_path="/root/workspace/ABOUTME.md")
)

# Lightweight image — used by scrape/evaluate/sheet/followups/prune
image = _base

# Playwright image — used by pipeline_generate_applications (Stage 4+5).
# Adds Chromium system deps + browser binary for CV HTML→PDF rendering.
# run_commands must come before add_local_* (Modal requirement).
image_with_playwright = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install(
        "libssl-dev", "ca-certificates", "libffi-dev",
        # Playwright Chromium system deps
        "libnss3", "libatk1.0-0", "libatk-bridge2.0-0",
        "libcups2", "libdrm2", "libxkbcommon0", "libxcomposite1",
        "libxdamage1", "libxfixes3", "libxrandr2", "libgbm1", "libasound2",
    )
    .pip_install_from_requirements("requirements.txt")
    .run_commands("python -m playwright install chromium")
    # Add local files last (Modal requirement when run_commands is used)
    .add_local_dir("execution", remote_path="/root/workspace/execution")
    .add_local_dir("directives", remote_path="/root/workspace/directives")
    .add_local_file("ABOUTME.md", remote_path="/root/workspace/ABOUTME.md")
    .add_local_file(
        ".tmp/CV_picture 24.06.2025_sizeAdjusted.jpg",
        remote_path="/root/workspace/cv_photo.jpg",
    )
)

# ---------------------------------------------------------------------------
# Persistent cache volume — stores .tmp/ data between runs
# ---------------------------------------------------------------------------
volume = modal.Volume.from_name("doe-tmp", create_if_missing=True)

WORKSPACE = Path("/root/workspace")
TMP_DIR = WORKSPACE / ".tmp"
ALERT_EMAIL = "simonobemair@gmail.com"

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
secrets = [
    modal.Secret.from_name("doe-google-oauth"),
    modal.Secret.from_name("doe-api-keys"),
]

app = modal.App("doe-pipeline", image=image)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Required Modal secret env vars; mapped to the file each must be decoded into.
_REQUIRED_OAUTH_SECRETS: dict[str, Path] = {
    "TOKEN_JSON":       WORKSPACE / "token.json",
    "TOKEN_GMAIL_JSON": WORKSPACE / "token_gmail.json",
    "CREDENTIALS_JSON": WORKSPACE / "credentials.json",
}
_REQUIRED_API_KEYS = ("OPEN_ROUTER_API_KEY", "GOOGLE_AI_STUDIO_API_KEY")


def _write_oauth_files() -> None:
    """Decode and write OAuth files from Modal secrets before each run.

    Validates that every required secret is present and base64-decodable
    BEFORE touching the filesystem, so a missing/rotated secret produces a
    clear `RuntimeError(f"Modal secret missing: {name}")` instead of a
    cryptic `binascii.Error` from later code paths.
    """
    missing_oauth = [name for name in _REQUIRED_OAUTH_SECRETS if not os.environ.get(name)]
    if missing_oauth:
        raise RuntimeError(
            f"Modal secret(s) missing: {', '.join(missing_oauth)}. "
            f"Check the doe-google-oauth secret in the Modal dashboard."
        )
    missing_api = [name for name in _REQUIRED_API_KEYS if not os.environ.get(name)]
    if missing_api:
        raise RuntimeError(
            f"Modal secret(s) missing: {', '.join(missing_api)}. "
            f"Check the doe-api-keys secret in the Modal dashboard."
        )

    for name, path in _REQUIRED_OAUTH_SECRETS.items():
        b64_value = os.environ[name]
        try:
            decoded = base64.b64decode(b64_value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(
                f"Modal secret {name} is not valid base64: {exc}. "
                f"Re-encode the token file (e.g. `base64 -i token.json | tr -d '\\n'`) "
                f"and update the secret in the Modal dashboard."
            ) from exc
        path.write_bytes(decoded)


def _send_email(subject: str, body: str, to: str = ALERT_EMAIL) -> bool:
    """Send a plain-text email via the shared gmail_send helper.

    Returns True on success, False on any failure (best-effort — never raises).
    """
    sys.path.insert(0, str(WORKSPACE))
    from execution.gmail_send import send_email as _send
    return _send(subject, body, to)


def _run(script: str, *args: str, stage_label: str | None = None) -> None:
    """Run an execution script as a subprocess, streaming output live.

    On non-zero exit:
      1. capture the last 40 output lines for context
      2. send a failure email to ALERT_EMAIL
      3. raise RuntimeError so Modal marks the run as failed
    """
    label = stage_label or script
    cmd = [sys.executable, f"execution/{script}", *args]

    proc = subprocess.Popen(
        cmd,
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout for unified ordering
        text=True,
        bufsize=1,
    )
    output_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            output_lines.append(line)
        proc.wait()
    except BaseException:
        proc.kill()
        raise

    if proc.returncode != 0:
        tail = "".join(output_lines[-40:]) or "(no output captured)"
        body = (
            f"Stage:        {label}\n"
            f"Script:       execution/{script} {' '.join(args)}\n"
            f"Exit code:    {proc.returncode}\n"
            f"Workspace:    {WORKSPACE}\n\n"
            f"Last 40 output lines:\n"
            f"{'-' * 60}\n"
            f"{tail}\n"
        )
        _send_email(
            subject=f"[DOE pipeline FAIL] {label}",
            body=body,
        )
        raise RuntimeError(f"{script} exited with code {proc.returncode}")


def _notify_applications_ready() -> None:
    """Send a Gmail notification after cover letters + CVs are generated.

    Reads the current APPLYING rows from the Sheet to build the email body.
    Best-effort — never raises.
    """
    if not (WORKSPACE / "token_gmail.json").exists():
        return

    try:
        sys.path.insert(0, str(WORKSPACE))
        from execution.write_jobs_to_sheet import authenticate as _auth, _sheets_api_call
        client = _auth()
        ws = _sheets_api_call(client.open, "Swiss Job Search Pipeline").sheet1
        rows = _sheets_api_call(ws.get_all_values)
        header = [h.strip() for h in rows[0]] if rows else []
        applying_jobs: list[str] = []
        if header:
            try:
                status_i = header.index("Status")
                title_i = header.index("Title")
                company_i = header.index("Company")
                for row in rows[1:]:
                    def _c(i: int, _row=row) -> str:
                        return _row[i].strip() if i < len(_row) else ""

                    if _c(status_i).lower() == "applying":
                        applying_jobs.append(f"{_c(company_i)}: {_c(title_i)}")
            except (ValueError, IndexError):
                pass

        count = len(applying_jobs)
        if count == 0:
            return

        lines = "\n".join(f"  • {j}" for j in applying_jobs)
        body = (
            f"{count} application(s) are ready in Google Drive.\n\n"
            f"{lines}\n\n"
            "Open Google Drive → DOE Applications to review before applying."
        )
        _send_email(
            subject=f"DOE: {count} application(s) ready to review",
            body=body,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[notify] skipped: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scheduled functions
# ---------------------------------------------------------------------------

# CET = UTC+1 (winter) / CEST = UTC+2 (summer).
# Targets: 17:00 CET → 15:00 UTC in summer, 16:00 UTC in winter.
# Using 15:00 UTC — fires at 17:00 during CEST season (Mar–Oct).

@app.function(
    schedule=modal.Cron("0 15 * * 2,4"),   # Tue + Thu 17:00 CEST
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=7200,
    max_containers=1,
)
def pipeline_scrape_write() -> None:
    """Stage 1-3: scrape → evaluate → write to Google Sheet."""
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("scrape_jobs.py", "--no-fetch-descriptions", stage_label="scrape_jobs")
    volume.commit()
    _run("evaluate_jobs.py", stage_label="evaluate_jobs")
    volume.commit()
    _run("write_jobs_to_sheet.py", stage_label="write_jobs_to_sheet")
    volume.commit()


@app.function(
    # Mon-Fri 06:00 UTC = 08:00 CEST (summer) / 07:00 CET (winter).
    # Multi-touch flow checks each row against 3/6/12-bd thresholds and only sends
    # what's actually due — running daily means a touch fires on the first business
    # day after its threshold instead of waiting up to 2 days for the next M/W/F.
    schedule=modal.Cron("0 6 * * 1-5"),
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=600,
    max_containers=1,
)
def pipeline_send_followups() -> None:
    """Stage 6: send multi-touch follow-up emails (3 / 6 / 12 business days)."""
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("send_followups.py", "--send", stage_label="send_followups")
    volume.commit()


@app.function(
    schedule=modal.Cron("45 3 * * *"),     # Daily 05:45 CEST
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=300,
    max_containers=1,
)
def pipeline_prune() -> None:
    """Maintenance: delete stale rows from Google Sheet."""
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("prune_stale_jobs.py", "--yes", stage_label="prune_stale_jobs")
    volume.commit()


@app.function(
    # Sun 16:00 UTC = 18:00 CEST (summer) / 17:00 CET (winter).
    # Sent on Sunday evening so the metrics cover the full Mon–Sun window and the
    # candidate sees them before Monday's working day starts.
    schedule=modal.Cron("0 16 * * 0"),
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=300,
    max_containers=1,
)
def pipeline_weekly_digest() -> None:
    """Email a weekly pipeline digest (read-only on the sheet)."""
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("weekly_digest.py", stage_label="weekly_digest")
    volume.commit()


@app.function(
    schedule=modal.Cron("0 */2 * * *"),    # Every 2 hours
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=3600,
    image=image_with_playwright,
    max_containers=1,
)
def pipeline_generate_applications() -> None:
    """Stage 4 + 4.5 + 5: cover letter + CV + contact discovery for Ready_to_Apply rows.

    Polls the Sheet every 2 hours. Skips rows already in APPLYING/Applied.
    Generated PDFs/DOCXs are uploaded to Google Drive (DOE Applications/).
    Sends an email notification when new applications are ready.
    CV photo is bundled in the image at /root/workspace/cv_photo.jpg.

    Order of operations:
      1. write OAuth files from secrets (fail fast on missing secret)
      2. preflight: verify every API surface BEFORE any LLM call
      3. cover-letter stage → commit (CL checkpoint persists even if CV fails)
      4. CV stage → commit
      5. contact-discovery stage 4.5 → commit (free email lookup, fail-soft)
      6. notify success → commit
    """
    os.environ.setdefault("CV_PHOTO_PATH", "/root/workspace/cv_photo.jpg")
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("preflight.py", "--cloud", stage_label="preflight")
    _run("generate_cover_letter.py", "--sheet-triggered", stage_label="generate_cover_letter")
    volume.commit()
    _run("generate_cv.py", "--sheet-triggered", stage_label="generate_cv")
    volume.commit()
    # Stage 4.5: free contact-email discovery for the rows we just generated docs for.
    # discover_contacts.py is fail-soft (always exits 0), so a slow site can't abort the
    # pipeline — at worst, those rows ship with empty Contact_Email and the next run retries.
    _run("discover_contacts.py", "--sheet-triggered", stage_label="discover_contacts")
    volume.commit()
    _notify_applications_ready()
    volume.commit()


# ---------------------------------------------------------------------------
# On-demand preflight (no schedule — invoke with `modal run`)
# ---------------------------------------------------------------------------

@app.function(
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=300,
    image=image_with_playwright,
    max_containers=1,
)
def preflight() -> None:
    """Standalone API connectivity check — run before any production change.

    Exercises every external API surface this pipeline depends on (Sheets read +
    write, Drive list/create/upload/delete, Gmail send, OpenRouter, Gemini)
    plus cloud-only sanity (volume mount, chromium, bundled assets, profile parse).

    Sends the per-check result table by email; raises on any failure so Modal
    marks the run as failed.

    Invoke:  modal run execution/modal_pipeline.py::preflight
    """
    os.environ.setdefault("CV_PHOTO_PATH", "/root/workspace/cv_photo.jpg")
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("preflight.py", "--cloud", stage_label="preflight")
    volume.commit()
