"""
Cloud-scheduled pipeline runner on Modal (serverless).

Runs all automatable stages 24/7 without the local PC needing to be on:
  - Tue + Thu 18:00 CEST: pipeline_full — end-to-end parity with the local
                          scripts/pipeline_full.cmd (prune → scrape → evaluate
                          → write_sheet → cover_letter --min-score 6 → cv
                          --min-score 6 → discover_contacts).
                          No manual sheet gate; selection is by score threshold.
                          Date freshness on subsequent days is handled by
                          pipeline_daily_maintenance (no re-stamp inside this run).
  - send_followups:       on-demand only (auto-schedule disabled 2026-05-15;
                          run via `modal run execution/modal_pipeline.py::pipeline_send_followups`)
  - Daily 01:00 UTC:      pipeline_daily_maintenance — prune stale rows, clean
                          orphan folders, and re-stamp every score>=6 cover letter to TODAY,
                          pushing refreshed PDF/DOCX to Drive so the user
                          always opens an up-to-date letter. Fires at 03:00
                          CEST (summer) / 02:00 CET (winter) so it finishes
                          well before the 05:45 local apply window.
  - weekly digest:        on-demand only (kept unscheduled to stay within
                          Modal's 5-cron free-plan cap)

Stage 7 (run_swarm / apply) stays local — requires Chrome + human review.

Status flow after a pipeline_full run: rows land at Status="New" with
CL_Generated=Yes / CV_Generated=Yes / Contact_Email filled where found.
The user flips Status → Ready_to_Apply manually before running the local
swarm, which then progresses APPLYING → Applied.

Reliability hardening (see .claude/plans/hi-how-are-you-enchanted-tulip.md):
  - Defensive secret validation (clear errors instead of binascii cracks)
  - Per-stage volume.commit() so partial progress survives a mid-run crash
  - Email alert on any stage failure (Gmail via SMTP + GMAIL_APP_PASSWORD)
  - Volume-resident single-flight lock for pipeline_full prevents concurrent
    invocations (cron + manual `modal run` + dashboard re-fire).
    max_containers=1 alone proved insufficient on 2026-05-11 when a phantom
    Run 2 evicted Run 1 mid-CV. Stale locks (>8h) auto-clear; manual
    override: `modal volume rm doe-tmp .pipeline_lock.json`.
  - On-demand preflight verifies API surfaces when explicitly requested; scheduled
    runs rely on per-stage checks to avoid noisy Gmail/LLM probes

Secrets required (set once in Modal dashboard):
  doe-google-oauth  →  TOKEN_JSON (base64 of token.json, Sheets+drive.file scope)
                        CREDENTIALS_JSON (base64 of credentials.json — OAuth client id/secret)
  doe-api-keys      →  OPEN_ROUTER_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
                        SERPAPI_API_KEY, SERPAPI_API_KEY2

Volume:
  doe-tmp  →  mounted at /root/workspace/.tmp/ (persists caches between runs)

Images:
  image                — lightweight: followups / prune / weekly digest
  image_with_playwright — adds Playwright Chromium for CV PDF rendering;
                          used by pipeline_full + pipeline_daily_maintenance

Deploy:   modal deploy execution/modal_pipeline.py
Dry-run:  modal run execution/modal_pipeline.py::preflight
          modal run execution/modal_pipeline.py::pipeline_full
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json as _json_mod
import os
import re
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Playwright image — used by pipeline_full + pipeline_daily_maintenance (PDF rendering).
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
        # TTF fonts for fpdf2 cover-letter rendering. Without this, _setup_fonts()
        # in CoverLetterPDF can't find LiberationSans-*.ttf, fpdf2 falls back to
        # the helvetica core font (latin-1), and rendering raises
        # FPDFUnicodeEncodingException on the "•" in subtitles.
        "fonts-liberation",
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
STAGE_LOG_DIR = TMP_DIR / "stage_logs"
ALERT_EMAIL = "simonobemair@gmail.com"
MODAL_CV_PHOTO_PATH = WORKSPACE / "cv_photo.jpg"

# Age cutoff for prune_stale_jobs (passed via --days). Aggressive vs the
# script default of 30d because Swiss listings typically die within 1-2
# weeks; keeping the cutoff tight shrinks the daily restamp working set
# at the source. Both pipeline_full and pipeline_daily_maintenance share
# this constant so the window can't drift between cron functions.
PRUNE_WINDOW_DAYS = "14"

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
# Gmail send is via SMTP+app-password (env vars GMAIL_ADDRESS, GMAIL_APP_PASSWORD
# in the doe-api-keys secret); no Gmail OAuth token is required anymore.
_REQUIRED_OAUTH_SECRETS: dict[str, Path] = {
    "TOKEN_JSON":       WORKSPACE / "token.json",
    "CREDENTIALS_JSON": WORKSPACE / "credentials.json",
}
_REQUIRED_API_KEYS = ("OPEN_ROUTER_API_KEY",)
# GOOGLE_AI_STUDIO_API_KEY was dropped from required keys on 2026-05-03 — the
# Gemini-via-Google-AI-Studio direct path was retired. Gemini-3-Flash now
# reaches us through OpenRouter (cover-letter stage), so a single OpenRouter
# key covers all three models. The leftover GOOGLE_AI_STUDIO_API_KEY entry in
# the doe-api-keys Modal secret is safe to delete on the next rotation cycle.


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


def _set_modal_cv_photo_path() -> None:
    """Force CV generation to use the private photo baked into the Modal image."""
    os.environ["CV_PHOTO_PATH"] = str(MODAL_CV_PHOTO_PATH)


def _send_email(subject: str, body: str, to: str = ALERT_EMAIL) -> bool:
    """Send a plain-text email via the shared gmail_send helper.

    Returns True on success, False on any failure (best-effort — never raises).
    """
    sys.path.insert(0, str(WORKSPACE))
    from execution.gmail_send import send_email as _send
    return _send(subject, body, to)


OAUTH_SENTINEL = TMP_DIR / "oauth_sentinel.json"
OAUTH_REAUTH_HOWTO = (
    "Re-auth steps:\n"
    "  1. python execution/write_jobs_to_sheet.py    # opens browser, refreshes token.json\n"
    "  2. PowerShell: [Convert]::ToBase64String([IO.File]::ReadAllBytes('token.json'))\n"
    "  3. Modal dashboard -> Secrets -> doe-google-oauth -> set TOKEN_JSON to the base64 value\n"
    "  4. modal deploy execution/modal_pipeline.py\n"
)


def _check_oauth_health() -> tuple[bool, str | None]:
    """Active health check on the Sheets OAuth chain.

    Returns:
        (True, None)            — token healthy, no action needed.
        (True, "<warning msg>") — soft warning, day-5 or day-6 since last rotation.
        (False, "<error msg>")  — Sheets read failed; caller should raise to fail loudly.

    Side effect: refreshes the on-Volume sentinel that tracks last rotation date.
    Sticky: warns at most once on day 5 and once on day 6, never spams.

    The "rotation" date is detected by hashing the decoded token.json — when the
    Modal secret is updated and re-deployed, the hash changes and the sentinel
    resets. This gives an accurate proxy for the refresh_token age that
    Google's 7-day rule revokes against (sensitive scopes, unverified app).
    """
    token_path = WORKSPACE / "token.json"
    h = hashlib.sha256(token_path.read_bytes()).hexdigest()[:16]
    today_iso = date.today().isoformat()

    rec = {"hash": h, "rotated_at": today_iso, "last_warn_day": -1}
    if OAUTH_SENTINEL.exists():
        try:
            prev = _json_mod.loads(OAUTH_SENTINEL.read_text())
            if prev.get("hash") == h:
                rec["rotated_at"] = prev.get("rotated_at", today_iso)
                rec["last_warn_day"] = prev.get("last_warn_day", -1)
            # else: secret rotated → fresh sentinel, last_warn_day reset to -1
        except Exception:
            pass

    days_old = (date.today() - date.fromisoformat(rec["rotated_at"])).days

    # Active end-to-end check: refresh + Sheet header read.
    try:
        sys.path.insert(0, str(WORKSPACE))
        from execution.write_jobs_to_sheet import authenticate, _sheets_api_call
        client = authenticate()
        ws = _sheets_api_call(client.open, "Swiss Job Search Pipeline").sheet1
        _sheets_api_call(ws.row_values, 1)
    except Exception as exc:
        OAUTH_SENTINEL.write_text(_json_mod.dumps(rec))
        return (False, f"OAuth/Sheets read failed (token age {days_old}d): {exc}")

    # Sticky warning at days 5 and 6 only — never spam.
    if days_old in (5, 6) and rec["last_warn_day"] != days_old:
        rec["last_warn_day"] = days_old
        OAUTH_SENTINEL.write_text(_json_mod.dumps(rec))
        return (True, f"OAuth token age {days_old}d — re-auth recommended within {7 - days_old}d")

    OAUTH_SENTINEL.write_text(_json_mod.dumps(rec))
    return (True, None)


def _send_oauth_alert(message: str, healthy: bool) -> None:
    """Email the user about OAuth health. Best-effort — never raises."""
    if healthy:
        subject = f"[DOE pipeline OAUTH WARN] {message}"
    else:
        subject = "[DOE pipeline OAUTH FAIL] re-auth required"
    body = f"{message}\n\n{OAUTH_REAUTH_HOWTO}"
    try:
        _send_email(subject=subject, body=body)
    except Exception as exc:
        print(f"[oauth] alert email failed: {exc}", file=sys.stderr)


# Single-flight lock for pipeline_full. Application-layer guard against
# concurrent invocations from cron + manual `modal run` + dashboard re-fire.
# Stored on the persistent volume so sibling containers can see it.
PIPELINE_LOCK_FILE = TMP_DIR / ".pipeline_lock.json"
PIPELINE_LOCK_STALE_HOURS = 8  # matches pipeline_full's 28800s function timeout


def _acquire_pipeline_lock(stage_name: str) -> None:
    """Acquire the single-flight lock for pipeline-style cron functions.

    Best-effort guard against concurrent invocations spaced > a few seconds
    apart. On 2026-05-11 Modal Run 1 (cron 17:00 CEST) was evicted mid-CV
    when a phantom Run 2 started at 18:19 CEST (1h+ later) despite
    max_containers=1 — exactly the case this lock catches.

    Known limitations (intentional, not bugs):
      - TOCTOU window: two containers calling _acquire within the same
        second can both pass the exists() check before either commits.
        Modal Volume commits are eventually consistent across containers;
        there is no compare-and-swap. Rare in practice given Modal's
        scheduler — the real-world failure mode is the 1h+ spaced
        re-fire, not simultaneous double-trigger.
      - SIGKILL / OOM: if Modal kills the container itself, the finally
        block in pipeline_full never runs and the lock persists. The
        PIPELINE_LOCK_STALE_HOURS (8h, matches function timeout) auto-clear
        is the only real backstop in that case. Repeated stale-clear
        emails are a signal to investigate cumulative kills.

    On conflict (fresh lock held): emails an alert and raises RuntimeError
    so Modal marks the invocation failed. On stale lock: clears it, emails
    a notice, proceeds. Caller MUST release via _release_pipeline_lock()
    in a finally block.
    """
    volume.reload()  # see commits from any sibling container
    if PIPELINE_LOCK_FILE.exists():
        try:
            held = _json_mod.loads(PIPELINE_LOCK_FILE.read_text())
        except Exception:
            held = {}
        age_hours: float | None = None
        started_at_iso = held.get("started_at", "")
        try:
            started_at = datetime.fromisoformat(started_at_iso)
            age_hours = (datetime.now(timezone.utc) - started_at).total_seconds() / 3600
        except Exception:
            pass

        if age_hours is None or age_hours < PIPELINE_LOCK_STALE_HOURS:
            held_pretty = _json_mod.dumps(held, indent=2)
            subject = f"[DOE pipeline LOCK] {stage_name} aborted — another run in progress"
            body = (
                f"{stage_name} invocation refused: lock held since {started_at_iso}.\n\n"
                f"Lock contents:\n{held_pretty}\n\n"
                f"If the holding run is stuck, clear the lock manually:\n"
                f"  modal volume rm doe-tmp .pipeline_lock.json\n\n"
                f"The lock auto-clears after {PIPELINE_LOCK_STALE_HOURS}h."
            )
            try:
                _send_email(subject=subject, body=body)
            except Exception as exc:
                print(f"[lock] alert email failed: {exc}", file=sys.stderr)
            raise RuntimeError(
                f"{stage_name} lock held since {started_at_iso}; aborting concurrent invocation"
            )

        # Stale — clear and proceed
        print(f"[lock] clearing stale lock (age={age_hours:.1f}h): {held}", file=sys.stderr)
        try:
            _send_email(
                subject=f"[DOE pipeline LOCK] stale lock cleared before {stage_name}",
                body=f"Found stale lock (age {age_hours:.1f}h):\n{_json_mod.dumps(held, indent=2)}",
            )
        except Exception:
            pass

    lock_payload = {
        "stage": stage_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "container_id": os.environ.get("MODAL_TASK_ID", ""),
        "function_call_id": os.environ.get("MODAL_FUNCTION_CALL_ID", ""),
    }

    # Atomic write: tempfile in same dir, then os.replace.
    PIPELINE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        dir=str(PIPELINE_LOCK_FILE.parent), prefix=".pipeline_lock.", suffix=".tmp",
    ) as tf:
        _json_mod.dump(lock_payload, tf, indent=2)
        tmp_path = Path(tf.name)
    os.replace(tmp_path, PIPELINE_LOCK_FILE)
    volume.commit()  # publish lock to sibling containers ASAP


def _release_pipeline_lock() -> None:
    """Release the single-flight lock. Best-effort — never raises.

    Called from the consumer's finally block so the lock clears even when
    a stage failure raises out of pipeline_full.
    """
    try:
        if PIPELINE_LOCK_FILE.exists():
            PIPELINE_LOCK_FILE.unlink()
            volume.commit()
    except Exception as exc:
        print(f"[lock] release failed (continuing): {exc}", file=sys.stderr)


def _pipeline_startup(stage_name: str, *, single_flight: bool = False) -> None:
    """Standard prelude for every cloud cron.

    1. Decode OAuth + API-key secrets from Modal env vars.
    2. Ensure tmp dir exists.
    3. Run OAuth health check — fail loudly if Sheets auth is broken,
       email a soft warning on day-5/6 token age.
    4. If single_flight=True: acquire the pipeline lock. Caller MUST
       release via _release_pipeline_lock() in a finally block.
    """
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    healthy, msg = _check_oauth_health()
    if not healthy:
        _send_oauth_alert(msg or "Sheets read failed", healthy=False)
        raise RuntimeError(f"[{stage_name}] OAuth health check failed: {msg}")
    if msg:
        _send_oauth_alert(msg, healthy=True)
    if single_flight:
        _acquire_pipeline_lock(stage_name)


def _mirror_tmp() -> None:
    """Mirror top-level .tmp/ files → Drive (DOE Data/) for local visibility.

    Called once at the end of each cron function so the user can browse pipeline
    state via Drive for Desktop without `modal volume get`. Best-effort — never
    raises; mirror failure must not break the parent stage.

    The double try/except (here + inside mirror_tmp_to_drive_safe) is deliberate:
    the inner one catches Drive/network failures during the mirror itself; this
    outer one guards against import-time failure (e.g. broken module in a
    deploy, or a future image that drops googleapiclient).
    """
    try:
        sys.path.insert(0, str(WORKSPACE))
        from execution.tmp_drive_mirror import mirror_tmp_to_drive_safe
        mirror_tmp_to_drive_safe(TMP_DIR)
    except Exception as exc:  # noqa: BLE001 — visibility-only, non-fatal
        print(f"[tmp-mirror] import/call failed: {exc}", file=sys.stderr)


def _commit_volume_best_effort(context: str) -> None:
    """Persist diagnostic files before raising from a failed stage."""
    try:
        volume.commit()
    except Exception as exc:  # noqa: BLE001 — diagnostics-only, non-fatal
        print(f"[volume] commit failed after {context}: {exc}", file=sys.stderr)


def _run(
    script: str,
    *args: str,
    stage_label: str | None = None,
    check: bool = True,
    timeout: int = 3600,
    capture: list[str] | None = None,
) -> None:
    """Run an execution script as a subprocess, streaming output live.

    Args:
        check:   If False, a non-zero exit logs and emails but does NOT raise —
                 the pipeline continues. Use for best-effort stages (e.g. hydrate)
                 whose failure should not block downstream work.
        timeout: Seconds before a watchdog kills the subprocess. Defaults to 3600
                 (60 min). A `threading.Timer` is used because the streaming-stdout
                 read loop blocks while the pipe is open — `proc.wait(timeout=)` on
                 its own can't unwind a hung child.
        capture: If provided, every output line is appended to this list in
                 addition to being printed. Used to extract per-stage stats for
                 the daily success summary email.

    On non-zero exit (and check=True): capture last 40 output lines, email
    ALERT_EMAIL, raise RuntimeError. On timeout: kill, email TIMEOUT subject,
    raise unconditionally (a hung stage is always a real failure).
    """
    label = stage_label or script
    cmd = [sys.executable, f"execution/{script}", *args]
    STAGE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "stage"
    stamp = datetime.now(ZoneInfo("Europe/Zurich")).strftime("%Y%m%d_%H%M%S")
    stage_log_path = STAGE_LOG_DIR / f"{stamp}_{safe_label}.log"
    stage_log = stage_log_path.open("w", encoding="utf-8", errors="replace")
    stage_log.write(f"$ {' '.join(cmd)}\n")
    stage_log.flush()

    proc = subprocess.Popen(
        cmd,
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout for unified ordering
        text=True,
        bufsize=1,
    )

    timed_out = False

    def _kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    output_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
            stage_log.write(line)
            stage_log.flush()
            output_lines.append(line)
            if capture is not None:
                capture.append(line)
        proc.wait()
    except BaseException:
        proc.kill()
        raise
    finally:
        watchdog.cancel()
        stage_log.write(f"\n[modal_pipeline] exit_code={proc.returncode} timed_out={timed_out}\n")
        stage_log.close()

    if timed_out:
        tail = "".join(output_lines[-40:]) or "(no output captured)"
        body = (
            f"Stage:        {label}\n"
            f"Script:       execution/{script} {' '.join(args)}\n"
            f"Exit code:    TIMEOUT (killed after {timeout}s)\n"
            f"Workspace:    {WORKSPACE}\n"
            f"Stage log:    {stage_log_path}\n\n"
            f"Last 40 output lines:\n"
            f"{'-' * 60}\n"
            f"{tail}\n"
        )
        _send_email(subject=f"[DOE pipeline TIMEOUT] {label}", body=body)
        _commit_volume_best_effort(f"timeout in {label}")
        raise RuntimeError(f"{script} timed out after {timeout}s")

    if proc.returncode != 0:
        tail = "".join(output_lines[-40:]) or "(no output captured)"
        body = (
            f"Stage:        {label}\n"
            f"Script:       execution/{script} {' '.join(args)}\n"
            f"Exit code:    {proc.returncode}\n"
            f"Workspace:    {WORKSPACE}\n"
            f"Stage log:    {stage_log_path}\n\n"
            f"Last 40 output lines:\n"
            f"{'-' * 60}\n"
            f"{tail}\n"
        )
        _send_email(
            subject=f"[DOE pipeline FAIL] {label}",
            body=body,
        )
        _commit_volume_best_effort(f"non-zero exit in {label}")
        if check:
            raise RuntimeError(f"{script} exited with code {proc.returncode}")
        else:
            print(
                f"[pipeline] {label} non-zero exit ({proc.returncode}) — "
                f"continuing (check=False)",
                flush=True,
            )


def _send_maintenance_summary(stage_outputs: dict[str, list[str]]) -> None:
    """Send a positive-confirmation email after pipeline_daily_maintenance succeeds.

    Parses the captured output from each stage to extract counts. The whole point
    is to make silent failure impossible: if this email doesn't arrive at the
    expected time, something is wrong (cron not firing, secrets rotated,
    pipeline dead). Best-effort — never raises.
    """
    def _last_match(stage: str, pattern: str) -> str:
        for line in reversed(stage_outputs.get(stage, [])):
            m = re.search(pattern, line)
            if m:
                return m.group(1)
        return "?"

    fetched          = _last_match("hydrate_volume_from_drive", r"fetched=(\d+)")
    hydrate_failed   = _last_match("hydrate_volume_from_drive", r"failed=(\d+)")
    hydrate_total    = _last_match("hydrate_volume_from_drive", r"candidates=(\d+)")
    cleanup_local    = _last_match("cleanup_orphan_application_folders", r"local_deleted=(\d+)")
    cleanup_drive    = _last_match("cleanup_orphan_application_folders", r"drive_deleted=(\d+)")
    cleanup_failed   = _last_match("cleanup_orphan_application_folders", r"failed=(\d+)")
    cleanup_aborts   = _last_match("cleanup_orphan_application_folders", r"safety_aborts=(\d+)")
    updated          = _last_match("update_cover_letter_dates", r"updated=(\d+)")
    errored          = _last_match("update_cover_letter_dates", r"errored=(\d+)")
    drive_failed     = _last_match("update_cover_letter_dates", r"drive_failed=(\d+)")
    coverage_match   = _last_match("update_cover_letter_dates", r"Coverage: ([\d.]+%)")
    # Drive reconcile counts (Round 4) — parsed from the prune_stale_jobs stage.
    rec_active   = _last_match("prune_stale_jobs", r"Drive reconcile: active=(\d+)")
    rec_orphans  = _last_match("prune_stale_jobs", r"orphans=(\d+)")
    rec_archived = _last_match("prune_stale_jobs", r"archived=(\d+)")
    rec_aborted  = _last_match("prune_stale_jobs", r"aborted=(\S+)")

    body = (
        f"Daily maintenance ran successfully.\n\n"
        f"  Cleanup:   local_deleted={cleanup_local}, drive_deleted={cleanup_drive}, failed={cleanup_failed}, safety_aborts={cleanup_aborts}\n"
        f"  Hydrate:   fetched={fetched}, failed={hydrate_failed}, candidates={hydrate_total}\n"
        f"  Re-stamp:  updated={updated}, errored={errored}, drive_failed={drive_failed}, coverage={coverage_match}\n"
        f"  Reconcile: active={rec_active}, orphans={rec_orphans}, archived={rec_archived}, aborted={rec_aborted}\n\n"
        f"Workspace: {WORKSPACE}\n"
    )
    try:
        _send_email(subject="[DOE pipeline OK] daily maintenance", body=body)
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[summary] email failed: {exc}", file=sys.stderr)


def _notify_applications_ready() -> None:
    """Send a Gmail notification after cover letters + CVs are generated.

    Reads the current APPLYING rows from the Sheet to build the email body.
    Best-effort — never raises.
    """
    if not os.environ.get("GMAIL_APP_PASSWORD"):
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
# Targets: 18:00 CEST → 16:00 UTC in summer, 17:00 UTC in winter.
# Using 16:00 UTC — fires at 18:00 during CEST season (Mar–Oct).

@app.function(
    schedule=modal.Cron("0 15 * * 1,3"),   # Mon + Wed 15:00 UTC = 17:00 CEST / 16:00 CET — finishes well before Tue/Thu 01:00 UTC maintenance
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=28800,                         # 8 h: stage budgets total 18 300 s with 10 500 s headroom for slow scrape/LLM stages
    image=image_with_playwright,           # CV stage needs Chromium
    max_containers=1,
)
def pipeline_full() -> None:
    """End-to-end Modal pipeline matching scripts/pipeline_full.cmd.

    Runs every stage back-to-back, no manual sheet gate:
      prune → scrape (full description fetch) → evaluate
      → write_sheet → generate_cover_letter --min-score 6
      → generate_cv --min-score 6
      → discover_contacts --limit 250 (cloud-only extra) → notify → mirror

    Selection is by score threshold (--min-score 6) reading scored_jobs.json
    from the volume. Status stays at 'New' after generation (CL/CV columns
    flip to 'Yes'); the user gates Ready_to_Apply manually before running
    the local swarm.

    Cover letters are stamped with today's date during generation, so no
    re-stamp step is needed here — pipeline_daily_maintenance keeps them
    fresh on subsequent days.

    Single-flight: acquires .pipeline_lock.json on the volume at start and
    releases it in finally — concurrent invocations (cron + manual + dash)
    get a "lock held" email and raise, the holding run continues unaffected.
    Stale locks (>8h) auto-clear; manual override is a `modal volume rm`.

    Contact discovery is split off into pipeline_discover_contacts (Tue+Thu
    06:00 UTC) so its slow per-row work cannot timeout-cascade and block
    notify + Drive mirror at the end of this run.

    Stage budgets total ~16 500 s under the 28 800 s (8 h) function timeout,
    leaving ~3 h 25 m headroom for slow scrape/LLM stages.

    Per-stage volume.commit() preserves partial progress on mid-run failure.
    On any stage's non-zero exit _run() emails the alert mailbox and raises.
    """
    _set_modal_cv_photo_path()
    _pipeline_startup("pipeline_full", single_flight=True)

    try:
        _run("prune_stale_jobs.py", "--yes", "--days", PRUNE_WINDOW_DAYS,
             "--archive-drive", "--reconcile-orphans",
             stage_label="prune_stale_jobs", timeout=1200)
        volume.commit()
        _run("scrape_jobs.py", stage_label="scrape_jobs", timeout=3600)
        volume.commit()
        _run("evaluate_jobs.py", stage_label="evaluate_jobs", timeout=3600)
        volume.commit()
        _run("write_jobs_to_sheet.py", stage_label="write_jobs_to_sheet", timeout=900)
        volume.commit()
        _run("generate_cover_letter.py", "--min-score", "6",
             stage_label="generate_cover_letter", timeout=3600)
        volume.commit()
        _run("generate_cv.py", "--min-score", "6", stage_label="generate_cv", timeout=3600)
        volume.commit()

        # Contact discovery runs separately as pipeline_discover_contacts —
        # its 250-row batch routinely exceeds 1h and the _run() watchdog
        # always raises on timeout (regardless of check=False), which used
        # to abort the notify + mirror below.
        _notify_applications_ready()
        _mirror_tmp()
    finally:
        _release_pipeline_lock()


@app.function(
    # Auto-schedule DISABLED 2026-05-15 — Contact_Email resolution sometimes
    # picks the wrong recipient, so follow-ups must be triggered manually after
    # the user verifies the contact. Re-enable by restoring:
    #   schedule=modal.Cron("0 6 * * 1-5"),
    # and re-running `modal deploy execution/modal_pipeline.py`.
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=600,
    max_containers=1,
)
def pipeline_send_followups(dry_run: bool = False) -> None:
    """Stage 6: send multi-touch follow-up emails (3 / 6 / 12 business days).

    On-demand run with no args sends. Pass `dry_run=True` via
    `modal run ... --dry-run` for a side-effect-free smoke test in cloud — runs
    the full eligibility scan + LLM generation but skips the Gmail send and
    sheet write. Useful for verifying OAuth + per-stage routing without firing
    real outbound mail.
    """
    _pipeline_startup("pipeline_send_followups")
    args = [] if dry_run else ["--send"]
    _run("send_followups.py", *args, stage_label="send_followups", timeout=540)
    volume.commit()
    _mirror_tmp()


@app.function(
    # Auto-schedule DISABLED 2026-05-17 to stay under Modal's 5-cron
    # free-plan cap (Modal allocates 2 slots per scheduled function, so
    # 3 scheduled functions = 6 > 5). Run on demand:
    #   modal run execution/modal_pipeline.py::pipeline_discover_contacts
    # Backlog drains via row-level checkpoint
    # (.tmp/discover_contacts_checkpoint.json, 24h TTL), so consecutive
    # manual runs resume cleanly without re-paying SERP/LLM cost.
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=7800,                          # 7200 s stage budget + 600 s startup/teardown margin
    image=image_with_playwright,           # Source 2 (impressum scrape) needs Chromium
    max_containers=1,
)
def pipeline_discover_contacts() -> None:
    """Stage 4.5 (split off): contact-email discovery for backlog rows.

    Split out of pipeline_full on 2026-05-16 because the 250-row batch
    routinely exceeded 1 h and the _run() watchdog always raises on timeout
    (check=False only suppresses non-zero exits, not timeouts), which used
    to skip notify + Drive mirror at the tail of pipeline_full.

    --limit 250 caps per-run work; the row-level checkpoint at
    .tmp/discover_contacts_checkpoint.json (24h TTL) lets consecutive runs
    drain the backlog without re-paying SERP/LLM cost.

    Single-flight: shares the .pipeline_lock.json mechanism with
    pipeline_full / daily_maintenance so concurrent runs cannot collide.
    """
    _pipeline_startup("pipeline_discover_contacts", single_flight=True)
    try:
        _run("discover_contacts.py", "--limit", "250",
             stage_label="discover_contacts", check=False, timeout=7200)
        volume.commit()
        _mirror_tmp()
    finally:
        _release_pipeline_lock()


@app.function(
    # Daily 01:00 UTC = 03:00 CEST (summer) / 02:00 CET (winter).
    # User starts applying ~05:45 local — this finishes well before then,
    # leaving a safe 2h+ buffer even if the run takes the full timeout.
    # One combined maintenance pass:
    #   1. prune stale Sheet rows
    #   2. cleanup orphan application folders absent from the Sheet
    #   3. hydrate the Volume from Drive (download any J-* folder Drive has but
    #      the Volume doesn't — bridges legacy local-generated folders)
    #   4. re-stamp every score>=6 cover letter to today's date and push the
    #      refreshed PDF/DOCX to Drive
    # Combined to stay under Modal's 5-cron cap on the current plan.
    schedule=modal.Cron("0 1 * * *"),
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=16800,                         # 4 h 40 m: stage budgets total 15 600 s with 1 200 s margin
    image=image_with_playwright,           # PDF render needs Chromium
    max_containers=1,
)
def pipeline_daily_maintenance() -> None:
    """Daily prune + orphan cleanup + Drive→Volume hydrate + CL date re-stamp.

    Stage 1: prune_stale_jobs.py --yes — drop expired rows from the Sheet.
    Stage 2: cleanup_orphan_application_folders.py --apply --yes — delete local
             and active Drive application folders no longer present in the
             Sheet. Runs fail-soft: safety trips alert but do not block re-stamp.
    Stage 3: hydrate_volume_from_drive.py --since-days 365 — download any
             DOE Applications/J-* folder Drive has but the Volume is missing.
             Without this, update_cover_letter_dates only sees Modal-generated
             folders and silently skips the legacy local-generated ones.
    Stage 4: update_cover_letter_dates.py --letter-date TODAY --min-score 6 —
             re-render PDF + DOCX with today's date and push refreshed pair to
             Drive (DOE Applications/{folder}/), overwriting older-dated copies.
             No LLM calls — body is extracted from the existing DOCX.
    """
    _pipeline_startup("pipeline_daily_maintenance")

    # Capture each stage's output so the success summary email can extract counts.
    outputs: dict[str, list[str]] = {
        "prune_stale_jobs": [],
        "cleanup_orphan_application_folders": [],
        "hydrate_volume_from_drive": [],
        "update_cover_letter_dates": [],
    }

    _run(
        "prune_stale_jobs.py", "--yes", "--days", PRUNE_WINDOW_DAYS,
        "--archive-drive", "--reconcile-orphans",
        stage_label="prune_stale_jobs",
        timeout=1200,                   # 20 min — Sheet ops + Drive archive/purge + orphan reconcile
        capture=outputs["prune_stale_jobs"],
    )
    volume.commit()
    _run(
        "cleanup_orphan_application_folders.py",
        "--targets", "both",
        "--apply", "--yes",
        "--grace-hours", "24",
        "--max-delete-pct", "0.40",       # 0.25 was tripping at ~25.6% steady-state backlog; 500-count cap still bounds blast radius
        "--max-delete-count", "500",
        stage_label="cleanup_orphan_application_folders",
        check=False,                    # safety trips should alert, not block re-stamp
        timeout=1200,                   # 20 min — expected near-zero after initial cleanup
        capture=outputs["cleanup_orphan_application_folders"],
    )
    volume.commit()
    _run(
        "hydrate_volume_from_drive.py",
        "--since-days", "365",
        stage_label="hydrate_volume_from_drive",
        check=False,                    # best-effort — never blocks re-stamp
        timeout=2400,                   # 40 min ceiling
        capture=outputs["hydrate_volume_from_drive"],
    )
    volume.commit()
    today = datetime.now(ZoneInfo("Europe/Zurich")).strftime("%Y-%m-%d")
    _run(
        "update_cover_letter_dates.py",
        "--letter-date", today,
        "--min-score", "6",
        # No --statuses filter: re-stamp every score>=6 row on Drive so the local
        # view stays in sync with the canonical source. Previously --statuses New
        # left older Applied/Ready_to_Apply rows un-stamped, causing the 06:33
        # local task to report 35% missing folders.
        stage_label="update_cover_letter_dates",
        timeout=10800,                  # 3 h ceiling — absorbs Drive throttling / backlog growth
        capture=outputs["update_cover_letter_dates"],
    )
    volume.commit()

    _send_maintenance_summary(outputs)
    _mirror_tmp()


@app.function(
    # Auto-schedule DISABLED 2026-05-17 to stay within Modal's 5-cron
    # free-plan cap. Run on demand:
    #   modal run execution/modal_pipeline.py::pipeline_weekly_digest
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=300,
    max_containers=1,
)
def pipeline_weekly_digest() -> None:
    """Email a weekly pipeline digest (read-only on the sheet)."""
    _pipeline_startup("pipeline_weekly_digest")
    _run("weekly_digest.py", stage_label="weekly_digest", timeout=270)
    volume.commit()
    _mirror_tmp()


# ---------------------------------------------------------------------------
# On-demand folder hygiene (no schedule)
# ---------------------------------------------------------------------------

@app.function(
    volumes={str(TMP_DIR): volume},
    secrets=secrets,
    timeout=3600,
    max_containers=1,
)
def cleanup_orphan_application_folders(
    targets: str = "both",
    apply: bool = False,
    force: bool = False,
    grace_hours: int = 24,
    max_delete_pct: float = 0.40,
    max_delete_count: int = 500,
) -> None:
    """Clean Modal Volume / Drive application folders absent from the Sheet.

    Dry-run by default. Pass `apply=True` to delete candidates. Large cleanups
    still trip the tool's count/percentage guards unless `force=True` is set.

    Invoke:
      modal run execution/modal_pipeline.py::cleanup_orphan_application_folders
      modal run execution/modal_pipeline.py::cleanup_orphan_application_folders --apply --force
    """
    if targets not in {"local", "drive", "both"}:
        raise ValueError("targets must be one of: local, drive, both")
    _pipeline_startup("cleanup_orphan_application_folders")
    args = [
        "--targets", targets,
        "--grace-hours", str(grace_hours),
        "--max-delete-pct", str(max_delete_pct),
        "--max-delete-count", str(max_delete_count),
    ]
    if apply:
        args.extend(["--apply", "--yes"])
    if force:
        args.append("--force")
    _run(
        "cleanup_orphan_application_folders.py",
        *args,
        stage_label="cleanup_orphan_application_folders",
        timeout=3300,
    )
    volume.commit()
    _mirror_tmp()


# ---------------------------------------------------------------------------
# On-demand preflight. Keep this unscheduled so the app stays within Modal's
# 5-cron free-plan cap; run manually before production changes when needed.
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
    _set_modal_cv_photo_path()
    _write_oauth_files()
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _run("preflight.py", "--cloud", stage_label="preflight", timeout=270)
    volume.commit()
    _mirror_tmp()
