#!/usr/bin/env python3
"""Email one summary when new apply-tier listings land in the tracker.

A+/A listings are invisible until Simon opens the Google Sheet. This closes the
loop: after ingest + scoring, bundle any new `decision='apply'` rows into a
SINGLE Gmail message and stamp them `notified_at` so they're never re-sent.

DRY RUN by default (mirrors the parent follow-up emailer) — pass `--send` to
deliver. The workflow threads its own `--dry-run` through instead.

send_email lives in the parent DOE AI Agent project, which exposes its own
`execution` package. Because this project shadows that package name, a plain
`import execution.gmail_send` would resolve here (no gmail_send) — so we load the
file by path. When it can't be loaded (standalone / CI), notification degrades
to a logged no-op and scoring is completely unaffected.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    init_db,
    now_iso,
)

NOTIFY_DECISION = "apply"
DEFAULT_SINCE = "7d"
DEFAULT_MAX_ROWS = 25
_PARENT_GMAIL_PATH = BASE_DIR.parent / "execution" / "gmail_send.py"
_SINCE_RE = re.compile(r"^\s*(\d+)\s*([dhw]?)\s*$", re.IGNORECASE)
_SINCE_UNITS = {"d": "days", "h": "hours", "w": "weeks"}


@dataclass(frozen=True)
class NotifyResult:
    candidates: int
    sent: bool
    notified_ids: tuple[int, ...] = ()
    reason: str = ""


def parse_since(value: str) -> datetime:
    """'7d' / '48h' / '2w' / bare day-count -> the UTC cutoff datetime."""
    match = _SINCE_RE.match(value or "")
    if not match:
        raise ValueError(
            f"Invalid --since {value!r}; use e.g. 7d, 48h, 2w, or a day count."
        )
    amount = int(match.group(1))
    unit = (match.group(2) or "d").lower()
    return datetime.now(UTC) - timedelta(**{_SINCE_UNITS[unit]: amount})


def _cutoff_iso(since: str) -> str:
    # Matches now_iso()'s UTC second-resolution format, so a lexicographic
    # `created_at >= cutoff` comparison is also chronological.
    return parse_since(since).replace(microsecond=0).isoformat()


def fetch_candidates(conn, since: str, max_rows: int) -> list[dict]:
    cutoff = _cutoff_iso(since)
    rows = conn.execute(
        """
        SELECT id, title, city, rent_chf, commute_class, commute_minutes,
               commute_mode, priority_score, url, canonical_url, move_in, created_at
        FROM listings
        WHERE decision = ?
          AND notified_at IS NULL
          AND created_at >= ?
        ORDER BY priority_score DESC, created_at DESC
        LIMIT ?
        """,
        (NOTIFY_DECISION, cutoff, max_rows),
    ).fetchall()
    return [dict(row) for row in rows]


def _commute_text(row: dict) -> str:
    mode = row.get("commute_mode") or "commute"
    minutes = row.get("commute_minutes")
    return f"{minutes} min ({mode})" if minutes is not None else str(mode)


def format_summary(rows: list[dict]) -> tuple[str, str]:
    count = len(rows)
    plural = "s" if count != 1 else ""
    subject = f"Apartment search — {count} new apply-tier listing{plural}"
    lines = [f"{count} new apply-tier listing{plural} near Root D4:", ""]
    for index, row in enumerate(rows, start=1):
        rent = f"CHF {row['rent_chf']}" if row.get("rent_chf") else "rent n/a"
        city = row.get("city") or "?"
        title = (row.get("title") or "Untitled listing").strip()
        url = row.get("url") or row.get("canonical_url") or "(no url)"
        move_in = f" · frei ab {row['move_in']}" if row.get("move_in") else ""
        klass = row.get("commute_class") or "?"
        lines.append(f"{index}. [{klass}] {title}")
        lines.append(f"   {city} · {rent} · {_commute_text(row)}{move_in}")
        lines.append(f"   {url}")
        lines.append("")
    lines.append("These are decision=apply rows — you still review and send each one.")
    return subject, "\n".join(lines)


def _load_send_email():
    """Load send_email from the parent project by file path, or None."""
    if not _PARENT_GMAIL_PATH.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("doe_gmail_send", _PARENT_GMAIL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # also loads the parent .env (GMAIL creds)
        return getattr(module, "send_email", None)
    except Exception as exc:  # noqa: BLE001 - never let notification break the run
        print(f"[listing_notifier] could not load parent send_email: {exc}", file=sys.stderr)
        return None


def _default_recipient() -> str | None:
    for key in ("APARTMENT_NOTIFY_TO", "GMAIL_ADDRESS"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _stamp_notified(conn, ids: list[int]) -> None:
    timestamp = now_iso()
    conn.executemany(
        "UPDATE listings SET notified_at = ? WHERE id = ?",
        [(timestamp, listing_id) for listing_id in ids],
    )
    conn.commit()


def notify_new_listings(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    since: str = DEFAULT_SINCE,
    max_rows: int = DEFAULT_MAX_ROWS,
    dry_run: bool = True,
    recipient: str | None = None,
) -> NotifyResult:
    """Email a single summary of new apply-tier rows. Stamps notified_at only on
    a confirmed send. Returns a NotifyResult; never raises on send failure."""
    with closing(connect(db_path)) as conn:
        init_db(conn)
        rows = fetch_candidates(conn, since, max_rows)
        if not rows:
            return NotifyResult(0, False, reason="no new apply-tier listings")
        ids = [int(row["id"]) for row in rows]
        if dry_run:
            return NotifyResult(len(rows), False, reason="dry-run")

        send_email = _load_send_email()
        if send_email is None:
            return NotifyResult(len(rows), False, reason="send_email unavailable (standalone)")
        to = recipient or _default_recipient()
        if not to:
            return NotifyResult(len(rows), False, reason="no recipient (set APARTMENT_NOTIFY_TO or GMAIL_ADDRESS)")

        subject, body = format_summary(rows)
        if not send_email(subject, body, to):
            return NotifyResult(len(rows), False, reason="send_email returned False")
        _stamp_notified(conn, ids)
        return NotifyResult(len(rows), True, tuple(ids), reason="sent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Email a single summary of new apply-tier listings.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite tracker path.")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="Look-back window: 7d, 48h, 2w, or a day count.")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--to", help="Recipient. Defaults to APARTMENT_NOTIFY_TO or GMAIL_ADDRESS.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                       help="Report candidates without sending (default).")
    group.add_argument("--send", dest="dry_run", action="store_false",
                       help="Actually send the summary email.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = notify_new_listings(
            args.db, since=args.since, max_rows=args.max_rows,
            dry_run=args.dry_run, recipient=args.to,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    verb = "notified" if result.sent else "would notify"
    print(f"listing_notifier: {result.candidates} candidate(s); {verb} ({result.reason}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
