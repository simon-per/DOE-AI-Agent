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
import html
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
               commute_mode, priority_score, url, canonical_url, move_in,
               message_draft, wg_fit_score, price_score, contact_email, source,
               recommended_action, decision, created_at, updated_at,
               openrouter_score, openrouter_reason
        FROM listings
        WHERE decision = ?
          AND notified_at IS NULL
          AND status NOT IN ('sent', 'archived', 'expired')
          AND (created_at >= ? OR updated_at >= ?)
        ORDER BY priority_score DESC, created_at DESC
        LIMIT ?
        """,
        (NOTIFY_DECISION, cutoff, cutoff, max_rows),
    ).fetchall()
    return [dict(row) for row in rows]


def _commute_text(row: dict) -> str:
    mode = row.get("commute_mode") or "commute"
    minutes = row.get("commute_minutes")
    return f"{minutes} min ({mode})" if minutes is not None else str(mode)


def _rent_text(row: dict) -> str:
    return f"CHF {row['rent_chf']}" if row.get("rent_chf") else "rent n/a"


def _headline(index: int, row: dict) -> str:
    title = (row.get("title") or "Untitled listing").strip()
    move_in = f" · frei ab {row['move_in']}" if row.get("move_in") else ""
    klass = row.get("commute_class") or "?"
    return f"{index}. [{klass}] {title} · {_rent_text(row)} · {_commute_text(row)}{move_in}"


def _meta_text(row: dict) -> str:
    wg = row.get("wg_fit_score")
    wg_text = f"WG-fit {wg}" if wg is not None else "WG-fit ?"
    return (
        f"   {row.get('city') or '?'} · {wg_text} · "
        f"price: {row.get('price_score') or '?'} · via {row.get('source') or '?'}"
    )


def _llm_line(row: dict) -> str:
    """Advisory LLM re-rank annotation, or '' when the row was not scored.
    Advisory only — it never reflects or changes the deterministic decision."""
    score = row.get("openrouter_score")
    if score in (None, ""):
        return ""
    reason = (row.get("openrouter_reason") or "").strip()
    return f"LLM {score} — {reason}" if reason else f"LLM {score}"


def format_summary(rows: list[dict]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body). Each listing is a complete action
    packet: headline, meta, apply link/contact, and the ready-to-send draft."""
    count = len(rows)
    plural = "s" if count != 1 else ""
    subject = f"Apartment search — {count} new apply-tier listing{plural}"

    text = [f"{count} new apply-tier listing{plural} near Root D4.", ""]
    for index, row in enumerate(rows, start=1):
        url = row.get("url") or row.get("canonical_url") or "(no url)"
        text.append(_headline(index, row))
        text.append(_meta_text(row))
        llm_line = _llm_line(row)
        if llm_line:
            text.append(f"   {llm_line}")
        text.append(f"   Apply: {url}")
        if row.get("contact_email"):
            text.append(f"   Email: {row['contact_email']}")
        text.append("   ----- ready-to-send draft -----")
        for line in (row.get("message_draft") or "").strip().splitlines():
            text.append(f"   {line}" if line else "")
        text.append("   --------------------------------")
        text.append("")
    text.append("decision=apply rows — review and send each one yourself.")

    return subject, "\n".join(text), _format_html(rows, count, plural)


def _format_html(rows: list[dict], count: int, plural: str) -> str:
    blocks = [
        f"<p>{count} new apply-tier listing{plural} near Root D4. "
        "You still review and send each one.</p>"
    ]
    for index, row in enumerate(rows, start=1):
        url = row.get("url") or row.get("canonical_url") or ""
        klass = html.escape(row.get("commute_class") or "?")
        title = html.escape((row.get("title") or "Untitled listing").strip())
        rent = html.escape(_rent_text(row))
        commute = html.escape(_commute_text(row))
        city = html.escape(row.get("city") or "?")
        wg = row.get("wg_fit_score")
        wg_text = f"WG-fit {wg}" if wg is not None else "WG-fit ?"
        price = html.escape(str(row.get("price_score") or "?"))
        source = html.escape(row.get("source") or "?")
        move_in = (
            f" · frei ab {html.escape(str(row['move_in']))}" if row.get("move_in") else ""
        )
        draft = html.escape((row.get("message_draft") or "").strip())
        apply_html = (
            f'<a href="{html.escape(url, quote=True)}">Apply &#9656;</a>'
            if url else "(no url)"
        )
        contact = ""
        if row.get("contact_email"):
            email = html.escape(row["contact_email"])
            contact = f' · <a href="mailto:{html.escape(row["contact_email"], quote=True)}">{email}</a>'
        llm = _llm_line(row)
        llm_html = f"<br>{html.escape(llm)}" if llm else ""
        blocks.append(
            f"<h3>{index}. [{klass}] {title}</h3>"
            f"<p>{city} · {rent} · {commute}{move_in}<br>"
            f"{wg_text} · price: {price} · via {source}{llm_html}<br>"
            f"{apply_html}{contact}</p>"
            f'<pre style="white-space:pre-wrap;border:1px solid #ddd;'
            f'padding:8px;border-radius:4px;">{draft}</pre>'
        )
    return '<div style="font-family:sans-serif;font-size:14px;">' + "".join(blocks) + "</div>"


_SEND_EMAIL_CACHE: list = []  # memo box: [] = unresolved, [callable|None] = resolved


def _load_send_email():
    """Load send_email from the parent project by file path, or None.

    Memoized so repeated --send calls in one process don't re-exec the parent
    module (and re-run its dotenv load) each time.
    """
    if _SEND_EMAIL_CACHE:
        return _SEND_EMAIL_CACHE[0]
    resolved = None
    if _PARENT_GMAIL_PATH.exists():
        try:
            spec = importlib.util.spec_from_file_location("doe_gmail_send", _PARENT_GMAIL_PATH)
            if spec is None or spec.loader is None:
                raise ImportError(f"no import spec for {_PARENT_GMAIL_PATH}")
            module = importlib.util.module_from_spec(spec)
            # Register before exec so the module can resolve itself if it ever
            # grows internal imports; also loads the parent .env (GMAIL creds).
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            resolved = getattr(module, "send_email", None)
        except Exception as exc:  # noqa: BLE001 - never let notification break the run
            print(f"[listing_notifier] could not load parent send_email: {exc}", file=sys.stderr)
            resolved = None
    _SEND_EMAIL_CACHE.append(resolved)
    return resolved


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
    preview: bool = False,
) -> NotifyResult:
    """Email a single action brief of new apply-tier rows. Stamps notified_at
    only on a confirmed send. Returns a NotifyResult; never raises on send
    failure. With preview=True, prints the rendered text body in dry-run."""
    with closing(connect(db_path)) as conn:
        init_db(conn)
        rows = fetch_candidates(conn, since, max_rows)
        if not rows:
            return NotifyResult(0, False, reason="no new apply-tier listings")
        ids = [int(row["id"]) for row in rows]
        if dry_run:
            if preview:
                subject, text_body, _ = format_summary(rows)
                print(f"Subject: {subject}\n")
                print(text_body)
            return NotifyResult(len(rows), False, reason="dry-run")

        send_email = _load_send_email()
        if send_email is None:
            return NotifyResult(len(rows), False, reason="send_email unavailable (standalone)")
        to = recipient or _default_recipient()
        if not to:
            return NotifyResult(len(rows), False, reason="no recipient (set APARTMENT_NOTIFY_TO or GMAIL_ADDRESS)")

        subject, text_body, html_body = format_summary(rows)
        if not send_email(subject, text_body, to, html=html_body):
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
    parser.add_argument("--preview", action="store_true",
                        help="In dry-run, print the rendered action brief instead of just the count.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                       help="Report candidates without sending (default).")
    group.add_argument("--send", dest="dry_run", action="store_false",
                       help="Actually send the summary email.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.preview:
        # The brief uses · and — ; keep them readable in a Windows console.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    try:
        result = notify_new_listings(
            args.db, since=args.since, max_rows=args.max_rows,
            dry_run=args.dry_run, recipient=args.to, preview=args.preview,
        )
    except ValueError as exc:
        raise SystemExit(str(exc))
    verb = "notified" if result.sent else "would notify"
    print(f"listing_notifier: {result.candidates} candidate(s); {verb} ({result.reason}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
