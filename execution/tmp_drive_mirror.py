"""
Mirror top-level .tmp/ files to Google Drive (DOE Data/) for local visibility.

The Modal cron pipeline persists working state on a Modal Volume that is invisible
to Drive for Desktop. This module pushes the JSON/TXT artifacts (raw_jobs.json,
scored_jobs.json, *_checkpoint.json, caches, quality_report.json, sentinels) to a
Drive folder so the user can browse them on Windows without `modal volume get`.

Behavior:
  - One-way: Modal/local .tmp/ → Drive. Nothing is read back.
  - Idempotent: existing Drive files are updated in place (same fileId).
  - Top-level only: subdirectories are skipped. `applications/` is already
    handled by drive_upload.py; `swarm/` and `browser_profile/` are local-only
    and not present on Modal.
  - Best-effort: any exception is logged and swallowed. The mirror is for
    visibility, not pipeline correctness — it must never fail a parent stage.

Usage (from modal_pipeline.py, once at the end of each cron function):
    from execution.tmp_drive_mirror import mirror_tmp_to_drive_safe
    mirror_tmp_to_drive_safe()
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.drive_upload import (  # noqa: E402  (sys.path mutation above)
    _MIME_MAP,
    _get_credentials,
    _get_or_create_folder,
)

log = logging.getLogger(__name__)

DRIVE_DATA_FOLDER = "DOE Data"
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB safety guardrail
MIRROR_SUFFIXES = {".json", ".txt"}  # canonical pipeline state outputs


def _default_tmp_dir() -> Path:
    """Resolve the .tmp directory for the current environment.

    Modal: /root/workspace/.tmp (mounted volume).
    Local: <repo root>/.tmp.
    """
    modal_tmp = Path("/root/workspace/.tmp")
    if modal_tmp.exists():
        return modal_tmp
    return PROJECT_ROOT / ".tmp"


def _select_files(tmp_dir: Path) -> list[Path]:
    """Pick top-level files in tmp_dir that should be mirrored.

    Skips: subdirectories, files outside MIRROR_SUFFIXES, files larger than
    MAX_FILE_BYTES (logs a warning so silent skips are visible).
    """
    selected: list[Path] = []
    for f in tmp_dir.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in MIRROR_SUFFIXES:
            continue
        size = f.stat().st_size
        if size > MAX_FILE_BYTES:
            log.warning(
                f"[tmp-mirror] skip oversize {f.name} ({size} bytes > {MAX_FILE_BYTES})"
            )
            continue
        selected.append(f)
    return selected


def _list_existing(service, folder_id: str) -> dict[str, str]:
    """List all files in `folder_id`, paginated. Returns {name: file_id}."""
    name_to_id: dict[str, str] = {}
    page_token: str | None = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            name_to_id[f["name"]] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return name_to_id


def _mirror_tmp_to_drive(tmp_dir: Path | None = None) -> str:
    """Mirror top-level files in `tmp_dir` to Drive's DOE Data/ folder.

    Returns the Drive folder URL (empty string if nothing to mirror).
    Raises on credential/network errors — production callers must use
    `mirror_tmp_to_drive_safe()` so a Drive outage cannot break a stage.
    """
    tmp_dir = tmp_dir or _default_tmp_dir()
    if not tmp_dir.exists():
        log.warning(f"[tmp-mirror] tmp dir not found: {tmp_dir}")
        return ""

    files_to_mirror = _select_files(tmp_dir)
    if not files_to_mirror:
        return ""

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    folder_id = _get_or_create_folder(service, DRIVE_DATA_FOLDER)
    name_to_id = _list_existing(service, folder_id)

    created, updated = 0, 0
    for path in sorted(files_to_mirror):
        mime = _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
        media = MediaFileUpload(str(path), mimetype=mime, resumable=False)
        existing_id = name_to_id.get(path.name)
        if existing_id:
            service.files().update(
                fileId=existing_id,
                media_body=media,
            ).execute()
            updated += 1
        else:
            service.files().create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            ).execute()
            created += 1

    folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
    log.info(
        f"[tmp-mirror] {created} new + {updated} updated → {folder_url}"
    )
    return folder_url


def mirror_tmp_to_drive_safe(tmp_dir: Path | None = None) -> None:
    """Best-effort wrapper: never raises. The only public entrypoint."""
    try:
        _mirror_tmp_to_drive(tmp_dir)
    except Exception as exc:  # noqa: BLE001 — visibility-only, non-fatal
        log.warning(f"[tmp-mirror] skipped: {exc}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url = _mirror_tmp_to_drive()
    if not url:
        print("Nothing to mirror.")
