"""
Google Drive upload helper for application folders.

Uploads a local .tmp/applications/{job_id}_{company}_{title}/ folder to
Google Drive under DOE Applications/{folder_name}/.

Uses the existing token.json (already has Drive scope from write_jobs_to_sheet.py).
Safe to call from Modal — credentials are decoded from secrets before each run.

Usage (called internally by generate_cover_letter.py + generate_cv.py):
    from execution.drive_upload import upload_application_folder
    drive_url = upload_application_folder(Path(".tmp/applications/J-abc123_Acme_CRM-Specialist"))
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger(__name__)

CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # drive.file = per-file scope: app can only see/manage files & folders
    # IT created. Non-sensitive — no 7-day refresh-token expiry.
    "https://www.googleapis.com/auth/drive.file",
]

DRIVE_ROOT_FOLDER = "DOE Applications"
ARCHIVE_FOLDER = "_Archive"

_MIME_FOLDER = "application/vnd.google-apps.folder"
_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".txt":  "text/plain",
    ".json": "application/json",
}

_DOWNLOAD_EXTENSIONS = {".docx", ".pdf", ".json"}

_RETRYABLE = {429, 500, 502, 503}

# Per-socket-op ceiling for the Drive client. httplib2 wires this into
# socket.settimeout(), so it caps each individual recv/send — NOT the wall-clock
# duration of a request. The realistic failure mode it rescues is a fully
# stalled socket (network blip, transient Google-side hang where the connection
# stays open but no bytes flow). A slow-but-progressing upload of a 200 KB PDF
# can still legally exceed 60 s without tripping this; for a true wall-cap on
# a request we'd need to wrap the call in a futures timeout, which we don't
# do today (stalled-socket is the case we've actually observed in production).
_DRIVE_HTTP_TIMEOUT_S = 60


def _gapi_retry(fn, *args, max_tries: int = 4, **kwargs):
    """Call a Google API callable with exponential backoff on transient errors (429/5xx)."""
    for attempt in range(max_tries):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            if exc.resp.status not in _RETRYABLE or attempt == max_tries - 1:
                raise
            sleep = 2 ** attempt  # 1 s, 2 s, 4 s
            log.warning(
                "[drive] HTTP %s — retry %d/%d in %ds",
                exc.resp.status, attempt + 1, max_tries - 1, sleep,
            )
            time.sleep(sleep)


def _build_drive_service(creds: Credentials):
    """Build the Drive client with a per-socket-op timeout.

    Default httplib2 socket timeout is unbounded — a single fully-stalled
    socket can pin a Modal stage to its outer wall (the subprocess watchdog
    can't kill a child stuck inside a blocking httplib2 read). Routing through
    AuthorizedHttp with an explicit timeout caps each recv/send at
    _DRIVE_HTTP_TIMEOUT_S. See the constant's comment for the caveat that
    this is per-syscall, not per-request.
    """
    authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_S))
    return build("drive", "v3", http=authed_http, cache_discovery=False)


def _get_credentials() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        except Exception:
            creds = None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        raise RuntimeError(
            "token.json missing or invalid — run write_jobs_to_sheet.py locally first "
            "to complete OAuth, then re-encode to Modal secret."
        )
    return creds


def _get_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Return the Drive folder ID for `name` under `parent_id`, creating it if needed."""
    query_parts = [
        f"name = '{name}'",
        f"mimeType = '{_MIME_FOLDER}'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")

    results = _gapi_retry(service.files().list(
        q=" and ".join(query_parts),
        spaces="drive",
        fields="files(id, name)",
    ).execute)

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    body: dict = {"name": name, "mimeType": _MIME_FOLDER}
    if parent_id:
        body["parents"] = [parent_id]
    folder = _gapi_retry(service.files().create(body=body, fields="id").execute)
    return folder["id"]


def upload_application_folder(
    local_folder: Path,
    only_files: Iterable[str] | None = None,
) -> str:
    """Upload files in `local_folder` to Drive under DOE Applications/.

    Creates DOE Applications/{local_folder.name}/ if it doesn't exist.
    By default uploads every file in the folder. Pass `only_files` (a set/list
    of basenames) to push just a subset — used by update_cover_letter_dates.py
    so it doesn't re-upload unchanged CV/ATS PDFs alongside the re-stamped CL.
    Returns the Drive web URL of the uploaded subfolder.
    """
    if not local_folder.exists():
        raise FileNotFoundError(f"Application folder not found: {local_folder}")

    # Skip dot-prefixed files in the default path. Application folders carry
    # internal sentinels like .drive_pushed_{date} (written by
    # update_cover_letter_dates.py) plus the occasional .DS_Store / .part
    # rollback temp; none of those belong in Drive. The only_files allowlist
    # path stays explicit and unaffected.
    files_to_upload = [
        f for f in local_folder.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]
    if only_files is not None:
        allow = set(only_files)
        files_to_upload = [f for f in files_to_upload if f.name in allow]
    if not files_to_upload:
        log.warning(f"[drive] No files to upload in {local_folder}")
        return ""

    creds = _get_credentials()
    service = _build_drive_service(creds)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    sub_id  = _get_or_create_folder(service, local_folder.name, parent_id=root_id)

    # Map existing names → fileId so we can update in place (re-rendered PDFs
    # with new letter dates must overwrite, not be skipped).
    existing = _gapi_retry(service.files().list(
        q=f"'{sub_id}' in parents and trashed = false",
        fields="files(id, name)",
    ).execute)
    existing_by_name = {f["name"]: f["id"] for f in existing.get("files", [])}

    created, updated = 0, 0
    for file_path in sorted(files_to_upload):
        mime = _MIME_MAP.get(file_path.suffix.lower(), "application/octet-stream")
        media = MediaFileUpload(str(file_path), mimetype=mime, resumable=False)
        existing_id = existing_by_name.get(file_path.name)
        if existing_id:
            _gapi_retry(service.files().update(
                fileId=existing_id,
                media_body=media,
            ).execute)
            updated += 1
            log.info(f"[drive] updated: {file_path.name}")
        else:
            _gapi_retry(service.files().create(
                body={"name": file_path.name, "parents": [sub_id]},
                media_body=media,
                fields="id",
            ).execute)
            created += 1
            log.info(f"[drive] uploaded: {file_path.name}")

    folder_url = f"https://drive.google.com/drive/folders/{sub_id}"
    log.info(f"[drive] {created} new + {updated} updated → {folder_url}")
    return folder_url


def list_application_folders() -> list[dict]:
    """Return [{'id', 'name', 'modifiedTime'}, ...] for every J-* folder under DOE Applications/.

    Uses pagination — no implicit cap. Folders not created by this app are invisible
    under the drive.file scope, which matches what we want (we only manage our own).
    """
    creds = _get_credentials()
    service = _build_drive_service(creds)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    folders: list[dict] = []
    page_token: str | None = None
    while True:
        resp = _gapi_retry(service.files().list(
            q=(
                f"'{root_id}' in parents "
                f"and mimeType = '{_MIME_FOLDER}' "
                "and trashed = false "
                "and name contains 'J-'"
            ),
            spaces="drive",
            fields="nextPageToken, files(id, name, modifiedTime)",
            pageSize=1000,
            pageToken=page_token,
        ).execute)
        for f in resp.get("files", []):
            if f["name"].startswith("J-"):
                folders.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def download_application_folder(folder_name: str, local_root: Path) -> Path | None:
    """Download Drive folder DOE Applications/{folder_name}/ into local_root/{folder_name}/.

    Returns the local Path on success, or None if the Drive folder doesn't exist.
    Idempotent: skips files whose local size already matches the Drive size.
    Only fetches .docx / .pdf / .json (skips Google-native docs and other types).
    """
    creds = _get_credentials()
    service = _build_drive_service(creds)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    resp = _gapi_retry(service.files().list(
        q=(
            f"name = '{folder_name}' "
            f"and mimeType = '{_MIME_FOLDER}' "
            f"and '{root_id}' in parents "
            "and trashed = false"
        ),
        spaces="drive",
        fields="files(id, name)",
    ).execute)
    matches = resp.get("files", [])
    if not matches:
        return None
    sub_id = matches[0]["id"]

    drive_files: list[dict] = []
    file_page_token: str | None = None
    while True:
        files_resp = _gapi_retry(service.files().list(
            q=f"'{sub_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, size, mimeType)",
            pageSize=1000,
            pageToken=file_page_token,
        ).execute)
        drive_files.extend(files_resp.get("files", []))
        file_page_token = files_resp.get("nextPageToken")
        if not file_page_token:
            break

    local_dir = local_root / folder_name
    local_dir.mkdir(parents=True, exist_ok=True)

    fetched = 0
    skipped = 0
    for f in drive_files:
        name = f["name"]
        ext = Path(name).suffix.lower()
        if ext not in _DOWNLOAD_EXTENSIONS:
            continue
        target = local_dir / name
        # Drive returns size as a string for binary files; missing for Google-native types.
        try:
            remote_size = int(f.get("size", "0"))
        except (TypeError, ValueError):
            remote_size = 0
        if target.exists() and remote_size and target.stat().st_size == remote_size:
            skipped += 1
            continue

        request = service.files().get_media(fileId=f["id"])
        tmp = target.with_suffix(target.suffix + ".part")
        with open(tmp, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=4 * 1024 * 1024)
            done = False
            while not done:
                _, done = _gapi_retry(downloader.next_chunk)
        tmp.replace(target)
        fetched += 1

    log.info(f"[drive] {folder_name}: {fetched} fetched, {skipped} skipped")
    return local_dir


def archive_folders_by_job_id(job_ids: list[str]) -> int:
    """Move every active DOE Applications/J-* folder whose ID prefix matches
    one of `job_ids` into DOE Applications/_Archive/.

    Stamps each archived folder's `appProperties.archived_at` (ISO-8601 UTC) so
    the purge stage uses a deterministic timestamp instead of relying on
    Drive's modifiedTime (which can be bumped by unrelated edits).

    Returns the count of folders moved. Idempotent — folders already in
    `_Archive/` aren't touched (`list_application_folders` only sees direct
    children of root). Per-folder failures are logged and skipped.
    """
    if not job_ids:
        return 0

    creds = _get_credentials()
    service = _build_drive_service(creds)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    archive_id = _get_or_create_folder(service, ARCHIVE_FOLDER, parent_id=root_id)

    job_id_set = {j for j in job_ids if j}
    if not job_id_set:
        return 0

    archived = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for f in list_application_folders():
        prefix = f["name"].split("_", 1)[0]
        if prefix not in job_id_set:
            continue
        try:
            _gapi_retry(service.files().update(
                fileId=f["id"],
                addParents=archive_id,
                removeParents=root_id,
                body={"appProperties": {
                    "archived_at": now_iso,
                    "archive_reason": "pruned",
                }},
                fields="id, parents, appProperties",
            ).execute)
            archived += 1
            log.info(f"[archive] {f['name']}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[archive] failed for {f['name']}: {exc}")
    return archived


def archive_orphan_drive_folders(
    sheet_job_ids: set[str],
    *,
    max_orphan_pct: float = 0.10,
    grace_hours: int = 12,
    force: bool = False,
) -> dict:
    """Archive every active J-* folder whose `job_id` prefix is NOT in `sheet_job_ids`.

    Returns a dict with keys:
        active            int   — active J-* folders inspected
        orphan_candidates int   — folders not in sheet (pre-grace, pre-threshold)
        in_grace          int   — orphan candidates skipped (modified within grace_hours)
        archived          int   — folders actually moved to _Archive/
        aborted           str|None — safeguard reason ('empty_sheet', 'threshold') or None
        failed            int   — per-folder API failures during the move (logged, counted)
        threshold_pct     float — the would-be orphan rate (after grace), for logging

    Layered safeguards (order matters):
      1. `sheet_job_ids` is empty → abort with 'empty_sheet' (no archives).
      2. (orphans_after_grace / active) > max_orphan_pct → abort with 'threshold'
         UNLESS `force=True`.
      3. Folders modified within `grace_hours` are bucketed as in_grace and skipped
         (covers the small race where a fresh CL upload commits to Drive seconds
         before its Sheet row).

    Stamps each archived folder with `appProperties`:
        archived_at    = ISO-8601 UTC now
        archive_reason = "orphan"

    `purge_archive_older_than(14)` (already wired) deletes these permanently
    after the 14-day window — same lifecycle as pruned-row archives.
    """
    result = {
        "active": 0,
        "orphan_candidates": 0,
        "in_grace": 0,
        "archived": 0,
        "aborted": None,
        "failed": 0,
        "threshold_pct": 0.0,
    }

    # Safeguard 1: empty sheet → assume read failure, refuse to mutate Drive.
    if not sheet_job_ids:
        result["aborted"] = "empty_sheet"
        log.error(
            "[reconcile] sheet_job_ids is empty — refusing to archive any folder. "
            "Likely a Sheet read error."
        )
        return result

    folders = list_application_folders()
    result["active"] = len(folders)
    if not folders:
        return result  # nothing to inspect, nothing to archive

    now = datetime.now(timezone.utc)
    grace_cutoff = now - timedelta(hours=grace_hours)

    # Bucket folders.
    orphans: list[dict] = []
    in_grace = 0
    for f in folders:
        prefix = f["name"].split("_", 1)[0]
        if prefix in sheet_job_ids:
            continue
        # Orphan candidate. Apply grace period.
        modified_iso = f.get("modifiedTime")
        is_in_grace = False
        if modified_iso:
            try:
                m = datetime.fromisoformat(modified_iso.replace("Z", "+00:00"))
                if m >= grace_cutoff:
                    is_in_grace = True
            except ValueError:
                pass  # unparseable timestamp → treat as not in grace
        if is_in_grace:
            in_grace += 1
        else:
            orphans.append(f)

    result["orphan_candidates"] = len(orphans) + in_grace
    result["in_grace"] = in_grace

    # Safeguard 2: threshold check.
    pct = len(orphans) / len(folders) if folders else 0.0
    result["threshold_pct"] = pct
    if pct > max_orphan_pct and not force:
        result["aborted"] = "threshold"
        log.error(
            f"[reconcile] would archive {len(orphans)}/{len(folders)} folders "
            f"({pct:.1%}) — exceeds safeguard threshold {max_orphan_pct:.0%}. "
            f"Aborting orphan cleanup. Pass --force-reconcile to override."
        )
        return result

    if not orphans:
        log.info(
            f"[reconcile] active={len(folders)} orphans=0 in_grace={in_grace} — nothing to archive"
        )
        return result

    # All safeguards passed (or forced) — archive the orphan set.
    creds = _get_credentials()
    service = _build_drive_service(creds)
    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    archive_id = _get_or_create_folder(service, ARCHIVE_FOLDER, parent_id=root_id)

    now_iso = now.isoformat()
    archived = 0
    failed = 0
    for f in orphans:
        try:
            _gapi_retry(service.files().update(
                fileId=f["id"],
                addParents=archive_id,
                removeParents=root_id,
                body={"appProperties": {
                    "archived_at": now_iso,
                    "archive_reason": "orphan",
                }},
                fields="id, parents, appProperties",
            ).execute)
            archived += 1
            log.info(f"[reconcile] orphan {f['name']} → _Archive/")
        except Exception as exc:  # noqa: BLE001 — per-folder failure must not abort batch
            failed += 1
            log.warning(f"[reconcile] failed for {f['name']}: {exc}")

    result["archived"] = archived
    result["failed"] = failed
    log.info(
        f"[reconcile] active={len(folders)} orphans={len(orphans)} "
        f"in_grace={in_grace} archived={archived} failed={failed}"
    )
    return result


def delete_drive_folders(folders: Iterable[dict]) -> dict:
    """Permanently delete explicit Drive folders by ID.

    This intentionally accepts concrete folder dicts from `list_application_folders`
    or a caller-side safety filter, not broad query parameters. The caller owns
    source-of-truth checks, thresholds, and grace periods before invoking this.
    Per-folder failures are logged and counted; later folders still run.
    """
    folder_list = [f for f in folders if f.get("id")]
    result = {
        "requested": len(folder_list),
        "deleted": 0,
        "failed": 0,
        "failed_items": [],
    }
    if not folder_list:
        return result

    creds = _get_credentials()
    service = _build_drive_service(creds)

    for f in folder_list:
        folder_id = f["id"]
        name = f.get("name", folder_id)
        try:
            _gapi_retry(service.files().delete(fileId=folder_id).execute)
            result["deleted"] += 1
            log.info(f"[drive-delete] deleted {name}")
        except Exception as exc:  # noqa: BLE001 - keep deleting independent folders
            result["failed"] += 1
            msg = f"{type(exc).__name__}: {exc}"
            result["failed_items"].append({"id": folder_id, "name": name, "error": msg})
            log.warning(f"[drive-delete] failed for {name}: {msg}")

    return result


def purge_archive_older_than(days: int = 14) -> tuple[int, int]:
    """Permanently delete folders in DOE Applications/_Archive/ whose
    `appProperties.archived_at` is older than `days` (UTC).

    Folders missing the archived_at stamp (legacy or hand-moved) fall back to
    Drive's modifiedTime as the age signal. Returns (deleted, kept).
    Per-folder delete failures are logged and counted as kept.
    """
    creds = _get_credentials()
    service = _build_drive_service(creds)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    archive_id = _get_or_create_folder(service, ARCHIVE_FOLDER, parent_id=root_id)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted, kept = 0, 0
    page_token: str | None = None

    while True:
        resp = _gapi_retry(service.files().list(
            q=f"'{archive_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, modifiedTime, appProperties)",
            pageSize=1000,
            pageToken=page_token,
        ).execute)
        for f in resp.get("files", []):
            archived_iso = (f.get("appProperties") or {}).get("archived_at")
            ref_iso = archived_iso or f.get("modifiedTime")
            if not ref_iso:
                kept += 1
                continue
            try:
                t = datetime.fromisoformat(ref_iso.replace("Z", "+00:00"))
            except ValueError:
                kept += 1
                continue
            if t < cutoff:
                try:
                    _gapi_retry(service.files().delete(fileId=f["id"]).execute)
                    deleted += 1
                    age_days = (datetime.now(timezone.utc) - t).days
                    log.info(f"[purge] deleted {f['name']} (age {age_days}d)")
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"[purge] failed for {f['name']}: {exc}")
                    kept += 1
            else:
                kept += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return deleted, kept
