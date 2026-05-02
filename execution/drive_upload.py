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
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger(__name__)

CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DRIVE_ROOT_FOLDER = "DOE Applications"

_MIME_FOLDER = "application/vnd.google-apps.folder"
_MIME_MAP = {
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".html": "text/html",
    ".txt":  "text/plain",
    ".json": "application/json",
}


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

    results = service.files().list(
        q=" and ".join(query_parts),
        spaces="drive",
        fields="files(id, name)",
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    body: dict = {"name": name, "mimeType": _MIME_FOLDER}
    if parent_id:
        body["parents"] = [parent_id]
    folder = service.files().create(body=body, fields="id").execute()
    return folder["id"]


def upload_application_folder(local_folder: Path) -> str:
    """Upload all files in `local_folder` to Drive under DOE Applications/.

    Creates DOE Applications/{local_folder.name}/ if it doesn't exist.
    Skips files that already exist in the Drive subfolder (idempotent).
    Returns the Drive web URL of the uploaded subfolder.
    """
    if not local_folder.exists():
        raise FileNotFoundError(f"Application folder not found: {local_folder}")

    files_to_upload = [f for f in local_folder.iterdir() if f.is_file()]
    if not files_to_upload:
        log.warning(f"[drive] No files to upload in {local_folder}")
        return ""

    creds = _get_credentials()
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    root_id = _get_or_create_folder(service, DRIVE_ROOT_FOLDER)
    sub_id  = _get_or_create_folder(service, local_folder.name, parent_id=root_id)

    # Get existing file names in subfolder to avoid re-uploading
    existing = service.files().list(
        q=f"'{sub_id}' in parents and trashed = false",
        fields="files(name)",
    ).execute()
    existing_names = {f["name"] for f in existing.get("files", [])}

    uploaded = []
    for file_path in sorted(files_to_upload):
        if file_path.name in existing_names:
            log.debug(f"[drive] skip (already exists): {file_path.name}")
            continue
        mime = _MIME_MAP.get(file_path.suffix.lower(), "application/octet-stream")
        media = MediaFileUpload(str(file_path), mimetype=mime, resumable=False)
        service.files().create(
            body={"name": file_path.name, "parents": [sub_id]},
            media_body=media,
            fields="id",
        ).execute()
        uploaded.append(file_path.name)
        log.info(f"[drive] uploaded: {file_path.name}")

    folder_url = f"https://drive.google.com/drive/folders/{sub_id}"
    if uploaded:
        log.info(f"[drive] {len(uploaded)} file(s) → {folder_url}")
    else:
        log.info(f"[drive] all files already present: {folder_url}")
    return folder_url
