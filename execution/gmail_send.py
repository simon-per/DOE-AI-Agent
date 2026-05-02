"""Shared Gmail-send helper — used by preflight checks and Modal pipeline alerts.

Looks for `token_gmail.json` at the project root first, falling back to the
Modal workspace path. Best-effort: returns False on any failure so callers
don't have to guard with try/except (notification must never abort the
pipeline).

Usage:
    from execution.gmail_send import send_email
    send_email("Subject line", "Body text", to="simonobemair@gmail.com")
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_TOKEN = PROJECT_ROOT / "token_gmail.json"
_MODAL_TOKEN = Path("/root/workspace/token_gmail.json")

GMAIL_SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.send",
]


def _resolve_token_file() -> Path | None:
    if _LOCAL_TOKEN.exists():
        return _LOCAL_TOKEN
    if _MODAL_TOKEN.exists():
        return _MODAL_TOKEN
    return None


def send_email(subject: str, body: str, to: str) -> bool:
    """Send a plain-text email via the Gmail OAuth credentials.

    Returns True on success, False on any failure (logs to stderr).
    """
    token_file = _resolve_token_file()
    if token_file is None:
        print(f"[email] skipped (no token_gmail.json found): {subject}", file=sys.stderr)
        return False
    try:
        import base64
        import email.mime.text as mime

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        msg = mime.MIMEText(body)
        msg["To"] = to
        msg["From"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"[email] failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return False
