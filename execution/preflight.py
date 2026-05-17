"""Preflight connection checks for the DOE AI Agent pipeline.

Verifies every external API surface the pipeline depends on, end-to-end.

Run before any production deploy. The Modal cron `pipeline_full` (Tue/Thu)
also runs this as its first step on every invocation, so a missing scope
or rotated token surfaces in the alert email *before* the LLM stage burns
tokens or generates partially-broken artifacts.

Usage:
    python execution/preflight.py             # local checks only
    python execution/preflight.py --cloud     # include cloud-only checks
                                              # (volume, chromium, bundled assets)

Exit codes:
    0  — all checks passed
    1  — at least one check failed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("preflight")

SPREADSHEET_NAME = "Swiss Job Search Pipeline"
DRIVE_ROOT_FOLDER = "DOE Applications"
PREFLIGHT_FOLDER_PREFIX = "_preflight_"
PREFLIGHT_TAB_NAME = "_preflight"
ALERT_EMAIL = "simonobemair@gmail.com"

# Cloud-only paths (set when running inside Modal)
WORKSPACE = Path("/root/workspace")
CLOUD_TMP = WORKSPACE / ".tmp"


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

def _run_check(name: str, fn: Callable[[], None]) -> dict:
    """Run a single check, capture its result."""
    log.info(f"[ ... ] {name}")
    start = time.time()
    try:
        fn()
        elapsed = time.time() - start
        log.info(f"[  OK ] {name} ({elapsed:.2f}s)")
        return {"name": name, "status": "OK", "reason": "", "elapsed": elapsed}
    except Exception as exc:  # noqa: BLE001 — checks fail in many ways
        elapsed = time.time() - start
        reason = f"{type(exc).__name__}: {exc}"
        log.error(f"[FAIL ] {name} ({elapsed:.2f}s) — {reason}")
        return {"name": name, "status": "FAIL", "reason": reason, "elapsed": elapsed}


def _print_summary(results: list[dict]) -> None:
    """Print a status table to stdout."""
    print("\n" + "=" * 78, flush=True)
    print(f"  PREFLIGHT REPORT — {datetime.now(timezone.utc).isoformat()}", flush=True)
    print("=" * 78, flush=True)
    name_w = max(len(r["name"]) for r in results) + 2
    for r in results:
        status_color = "OK" if r["status"] == "OK" else "FAIL"
        line = f"  [{status_color:>4}] {r['name']:<{name_w}} {r['elapsed']:>6.2f}s"
        if r["reason"]:
            line += f"   {r['reason']}"
        print(line, flush=True)
    print("=" * 78, flush=True)
    failed = [r for r in results if r["status"] == "FAIL"]
    if failed:
        print(f"  {len(failed)} check(s) FAILED. Pipeline will not proceed safely.", flush=True)
    else:
        print(f"  All {len(results)} check(s) passed.", flush=True)
    print("=" * 78 + "\n", flush=True)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_sheets_read() -> None:
    """Confirm we can open the spreadsheet and find the expected columns."""
    from execution.write_jobs_to_sheet import authenticate, _sheets_api_call

    client = authenticate()
    spreadsheet = _sheets_api_call(client.open, SPREADSHEET_NAME)
    ws = spreadsheet.sheet1
    rows = _sheets_api_call(ws.get_all_values)
    if not rows:
        raise RuntimeError(f"Spreadsheet '{SPREADSHEET_NAME}' is empty (no header row)")
    header = [h.strip() for h in rows[0]]
    expected = ["Status", "URL", "CL_Generated", "CV_Generated", "CL_Quality_Score", "Contact_Email"]
    missing = [c for c in expected if c not in header]
    if missing:
        raise RuntimeError(f"Required columns missing from header: {missing}")
    log.info(f"  Sheet has {len(rows) - 1} data row(s); all expected columns present")


def check_sheets_write() -> None:
    """Write a timestamp to a dedicated _preflight tab and read it back."""
    from execution.write_jobs_to_sheet import authenticate, _sheets_api_call

    client = authenticate()
    spreadsheet = _sheets_api_call(client.open, SPREADSHEET_NAME)
    try:
        tab = _sheets_api_call(spreadsheet.worksheet, PREFLIGHT_TAB_NAME)
    except Exception:
        tab = _sheets_api_call(
            spreadsheet.add_worksheet, title=PREFLIGHT_TAB_NAME, rows=2, cols=2
        )
    timestamp = datetime.now(timezone.utc).isoformat()
    _sheets_api_call(tab.update_acell, "A1", timestamp)
    readback = _sheets_api_call(tab.acell, "A1").value
    if readback != timestamp:
        raise RuntimeError(f"Sheet write didn't round-trip: wrote '{timestamp}', read '{readback}'")


def check_drive_list() -> None:
    """Confirm Drive auth works.

    Under drive.file scope the script can only see files it created, so the
    DOE Applications folder may not exist yet on a first run. We verify auth
    by issuing any successful files().list — content is irrelevant.
    """
    from googleapiclient.discovery import build

    from execution.drive_upload import _MIME_FOLDER, _get_credentials

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    results = service.files().list(
        q=(
            f"name = '{DRIVE_ROOT_FOLDER}' "
            f"and mimeType = '{_MIME_FOLDER}' "
            f"and trashed = false"
        ),
        spaces="drive",
        fields="files(id, name)",
        pageSize=1,
    ).execute()
    files = results.get("files", [])
    if files:
        log.info(f"  Drive auth OK; '{DRIVE_ROOT_FOLDER}' folder visible (id={files[0]['id']})")
    else:
        log.info(
            f"  Drive auth OK; '{DRIVE_ROOT_FOLDER}' not yet visible under drive.file scope "
            "(will be created on first upload)."
        )


def check_drive_write_cycle() -> None:
    """Create temp folder → upload file → verify size → delete folder."""
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    from execution.drive_upload import _get_credentials, _get_or_create_folder

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_name = f"{PREFLIGHT_FOLDER_PREFIX}{timestamp}"
    temp_id = _get_or_create_folder(service, temp_name, parent_id=root_id)

    cleanup_ok = False
    try:
        content = f"preflight check {timestamp}".encode()
        media = MediaInMemoryUpload(content, mimetype="text/plain")
        upload = service.files().create(
            body={"name": "preflight.txt", "parents": [temp_id]},
            media_body=media,
            fields="id,size",
        ).execute()
        size = int(upload.get("size", 0))
        if size != len(content):
            raise RuntimeError(
                f"Drive upload size mismatch: wrote {len(content)} bytes, server reports {size}"
            )
        log.info(f"  Created '{temp_name}', uploaded preflight.txt ({size} bytes)")
    finally:
        try:
            service.files().delete(fileId=temp_id).execute()
            cleanup_ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Cleanup of {temp_name} failed: {exc}")

    if not cleanup_ok:
        raise RuntimeError(f"Failed to delete preflight folder {temp_name}")


def check_gmail_send() -> None:
    """Send a tiny preflight ping email."""
    from execution.gmail_send import send_email

    timestamp = datetime.now(timezone.utc).isoformat()
    success = send_email(
        subject=f"[DOE preflight] {timestamp} OK",
        body=(
            f"Preflight gmail.send check at {timestamp}.\n\n"
            "If you see this in your inbox, Gmail OAuth + send scope are working.\n"
            "(Safe to ignore / delete.)"
        ),
        to=ALERT_EMAIL,
    )
    if not success:
        raise RuntimeError("send_email() returned False (see stderr above for details)")


def _check_openrouter_model(model: str) -> None:
    """Hit the OpenRouter completion endpoint with a small ping for one model.

    max_tokens=200: thinking models (DeepSeek-v4-flash) can spend 100-200
    tokens on hidden reasoning before emitting any content. Lower budgets
    produced stochastic empty responses (~10% miss rate). 200 tokens at
    deepseek's $0.28/M output rate is ~$0.00006 per ping, negligible.
    """
    from execution.llm_client import call_openrouter, get_openrouter_key

    api_key = get_openrouter_key()
    if not api_key:
        raise RuntimeError("OPEN_ROUTER_API_KEY not set in env")
    response = call_openrouter(api_key, prompt="say hi", temperature=0.0, max_tokens=200, model=model)
    if not response or not response.strip():
        raise RuntimeError(f"OpenRouter ({model}) returned empty response: {response!r}")
    log.info(f"  {model} responded ({len(response)} char(s))")


def check_openrouter_cover_letter() -> None:
    """Ping the cover-letter primary model (gemini-3-flash-preview by default)."""
    from execution.llm_client import MODEL_COVER_LETTER
    _check_openrouter_model(MODEL_COVER_LETTER)


def check_openrouter_cheap() -> None:
    """Ping the cheap-stage primary model (deepseek-v4-flash by default)."""
    from execution.llm_client import MODEL_CHEAP
    _check_openrouter_model(MODEL_CHEAP)


def check_openrouter_fallback() -> None:
    """Ping the universal fallback model (qwen3-235b-a22b-2507 by default)."""
    from execution.llm_client import MODEL_FALLBACK
    _check_openrouter_model(MODEL_FALLBACK)


def check_profile_parse() -> None:
    """Parse ABOUTME.md and confirm essential personal fields are populated."""
    from execution.profile_loader import load_profile

    profile = load_profile()
    if not profile.personal.name:
        raise RuntimeError("profile.personal.name is empty — ABOUTME.md parsing regression?")
    if not profile.personal.email:
        raise RuntimeError("profile.personal.email is empty — ABOUTME.md parsing regression?")
    if not profile.experience.role or not profile.experience.company:
        raise RuntimeError(
            "profile.experience.role or .company is empty — ABOUTME.md parsing regression?"
        )
    log.info(
        f"  Profile parsed: {profile.personal.name} — "
        f"{profile.experience.role} @ {profile.experience.company}"
    )


# Cloud-only checks ---------------------------------------------------------

def check_volume_sanity() -> None:
    """Write a tiny file to the .tmp/ volume and read it back."""
    target = CLOUD_TMP / "preflight.json"
    CLOUD_TMP.mkdir(parents=True, exist_ok=True)
    payload = f'{{"timestamp": "{datetime.now(timezone.utc).isoformat()}"}}'
    target.write_text(payload, encoding="utf-8")
    readback = target.read_text(encoding="utf-8")
    if readback != payload:
        raise RuntimeError(f"Volume round-trip failed: wrote {len(payload)}, read {len(readback)}")


def check_chromium_launch() -> None:
    """Launch headless chromium briefly to confirm the image is set up."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content("<html><body>preflight</body></html>")
        title = page.title()
        browser.close()
    log.info(f"  Chromium launched and rendered (title='{title}')")


def check_bundled_assets() -> None:
    """Confirm files baked into image_with_playwright are present and non-empty."""
    expected = [
        WORKSPACE / "ABOUTME.md",
        WORKSPACE / "cv_photo.jpg",
        WORKSPACE / "execution" / "cv_template.html",
        WORKSPACE / "execution" / "cv_template_ats.html",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    if missing:
        raise RuntimeError(f"Bundled asset(s) missing: {missing}")
    empty = [str(p) for p in expected if p.stat().st_size == 0]
    if empty:
        raise RuntimeError(f"Bundled asset(s) empty: {empty}")
    log.info(f"  All {len(expected)} bundled asset(s) present and non-empty")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="DOE pipeline preflight checks")
    parser.add_argument(
        "--cloud",
        action="store_true",
        help="Include cloud-only checks (volume, chromium, bundled assets)",
    )
    parser.add_argument(
        "--email-on-fail",
        action="store_true",
        help="Send the failure summary by email (always on in --cloud)",
    )
    args = parser.parse_args()

    log.info(f"Preflight starting (cloud={args.cloud})")

    results: list[dict] = []

    # Order matters: cheap → expensive, so we fail fast on auth issues
    results.append(_run_check("Profile parse (ABOUTME.md)",  check_profile_parse))
    results.append(_run_check("Sheets API: read",            check_sheets_read))
    results.append(_run_check("Sheets API: write",           check_sheets_write))
    results.append(_run_check("Drive API: list",             check_drive_list))
    results.append(_run_check("Drive API: create+upload+delete", check_drive_write_cycle))
    results.append(_run_check("Gmail API: send",             check_gmail_send))
    results.append(_run_check("OpenRouter LLM: cover_letter model", check_openrouter_cover_letter))
    results.append(_run_check("OpenRouter LLM: cheap model",        check_openrouter_cheap))
    results.append(_run_check("OpenRouter LLM: fallback model",     check_openrouter_fallback))

    if args.cloud:
        results.append(_run_check("Volume sanity (.tmp write/read)", check_volume_sanity))
        results.append(_run_check("Chromium launch",                 check_chromium_launch))
        results.append(_run_check("Bundled assets present",          check_bundled_assets))

    _print_summary(results)

    failed = [r for r in results if r["status"] == "FAIL"]
    if failed and (args.email_on_fail or args.cloud):
        # Build a plain-text version of the report for the alert email
        from execution.gmail_send import send_email  # local import keeps top of file fast

        lines = [
            f"PREFLIGHT FAILED — {datetime.now(timezone.utc).isoformat()}",
            f"{len(failed)} of {len(results)} check(s) failed.",
            "",
            "Failed checks:",
        ]
        for r in failed:
            lines.append(f"  - {r['name']}: {r['reason']}")
        lines.append("")
        lines.append("Full report:")
        for r in results:
            lines.append(f"  [{r['status']:>4}] {r['name']:<40} {r['elapsed']:>6.2f}s  {r['reason']}")
        send_email(
            subject=f"[DOE preflight FAIL] {len(failed)}/{len(results)} check(s)",
            body="\n".join(lines),
            to=ALERT_EMAIL,
        )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
