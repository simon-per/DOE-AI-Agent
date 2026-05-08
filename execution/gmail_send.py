"""Shared mail-send helper — used by preflight checks, modal_pipeline alerts,
and send_followups.

Sends via SMTP using a Google account app password (NOT OAuth). App passwords
do not expire and bypass Google's 7-day refresh-token rule for unverified
sensitive scopes, eliminating the most common cause of pipeline failure.

Required env vars (load from .env locally, Modal secret in cloud):
  GMAIL_ADDRESS       — your Google account email (e.g. "name@gmail.com")
  GMAIL_APP_PASSWORD  — 16-char app password from
                        https://myaccount.google.com/apppasswords

Usage:
    from execution.gmail_send import send_email
    send_email("Subject line", "Body text", to="someone@example.com")
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Allow .env to populate env vars on local runs (Modal injects them directly).
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def send_email(subject: str, body: str, to: str, html: str | None = None) -> bool:
    """Send a plain-text (or multipart) email via Gmail SMTP + app password.

    Returns True on success, False on any failure (logs to stderr).
    """
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not address or not password:
        print(
            "[email] skipped (GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set): "
            f"{subject}",
            file=sys.stderr,
        )
        return False

    try:
        if html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = address
        msg["To"] = to
        msg["Subject"] = subject

        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.sendmail(address, [to], msg.as_string())
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort; mail must never abort pipeline
        print(f"[email] failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return False
