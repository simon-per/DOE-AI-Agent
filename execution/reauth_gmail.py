"""One-shot helper to (re)generate token_gmail.json via OAuth consent.

Run when the existing refresh token is revoked (invalid_grant). Opens a browser,
prompts for consent, writes a fresh token at the project root.

Usage:  python execution/reauth_gmail.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = PROJECT_ROOT / "credentials.json"
TOKEN = PROJECT_ROOT / "token_gmail.json"

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> int:
    if not CREDENTIALS.exists():
        print(f"[reauth] credentials.json missing at {CREDENTIALS}", file=sys.stderr)
        return 1

    if TOKEN.exists():
        backup = TOKEN.with_suffix(".json.expired")
        TOKEN.replace(backup)
        print(f"[reauth] moved old token to {backup.name}")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print(f"[reauth] new token written to {TOKEN}")
    print(f"[reauth] refresh_token present: {bool(creds.refresh_token)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
