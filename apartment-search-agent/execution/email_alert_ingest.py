#!/usr/bin/env python3
"""Ingest portal saved-search alert emails into the apartment tracker.

Reads Gmail via IMAP (app password, read-only — never modifies server-side
labels or flags), routes each alert message to the registered portal parser,
extracts listing URLs + context, and hands them to apartment_pipeline for
normalization, scoring, and dedupe.

This script is the compliant alternative to scraping bot-protected portals
(Homegate / Newhome / ImmoScout24 / WGZimmer) — each portal supports free
saved-search email alerts which we ingest instead of hitting their HTML.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import os
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Iterator


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DB_PATH,
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    score_listing,
    upsert_listing,
)
from execution.email_parsers import REGISTERED_PARSERS, parser_for_sender  # noqa: E402

DEFAULT_MAILBOX = "INBOX"
DEFAULT_DAYS = 30
DEFAULT_BATCH_LIMIT = 200
IMAP_HOST = "imap.gmail.com"
UNROUTED_DUMP_DIR = BASE_DIR / ".tmp" / "email_samples"


@dataclass
class IngestStats:
    fetched: int = 0
    routed: int = 0
    unrouted: int = 0
    listings_seen: int = 0
    listings_created: int = 0
    listings_updated: int = 0
    listings_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _load_env() -> None:
    """Pull GMAIL_* from the apartment-search-agent .env, then fall back to
    the parent DOE AI Agent .env where the credentials are already set."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")


def imap_credentials() -> tuple[str, str]:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    if not address or not password:
        raise SystemExit(
            "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set "
            "(local .env or parent .env). See README.md."
        )
    return address, password


def init_email_seen_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS email_alert_seen (
            message_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            listings_created INTEGER NOT NULL DEFAULT 0,
            processed_at TEXT NOT NULL
        );
        """
    )


def already_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM email_alert_seen WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def record_seen(
    conn: sqlite3.Connection,
    message_id: str,
    source: str,
    listings_created: int,
) -> None:
    conn.execute(
        """
        INSERT INTO email_alert_seen (message_id, source, listings_created, processed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            listings_created = excluded.listings_created,
            processed_at     = excluded.processed_at
        """,
        (message_id, source, listings_created, datetime.now(UTC).isoformat()),
    )


def connect_imap(host: str, address: str, password: str, mailbox: str) -> imaplib.IMAP4_SSL:
    client = imaplib.IMAP4_SSL(host)
    client.login(address, password)
    status, _ = client.select(mailbox, readonly=True)
    if status != "OK":
        client.logout()
        raise SystemExit(f"Could not select mailbox {mailbox!r}: status={status}")
    return client


def imap_date(days_back: int) -> str:
    since = datetime.now(UTC) - timedelta(days=days_back)
    return since.strftime("%d-%b-%Y")


def search_alerts(client: imaplib.IMAP4_SSL, since_date: str, limit: int) -> list[bytes]:
    """Search for messages from any registered portal sender within the window."""
    matches: list[bytes] = []
    seen_domains: set[str] = set()
    for parser in REGISTERED_PARSERS:
        for domain in parser.imap_search_domains:
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            status, data = client.search(None, f'(SINCE {since_date}) (FROM "{domain}")')
            if status != "OK" or not data:
                continue
            matches.extend((data[0] or b"").split())
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break
    # Dedupe while preserving order, then cap.
    seen: set[bytes] = set()
    ordered: list[bytes] = []
    for uid in matches:
        if uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)
    return ordered[:limit]


def fetch_message(client: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
    status, data = client.fetch(uid, "(BODY.PEEK[])")
    if status != "OK" or not data or not data[0]:
        return None
    payload = data[0]
    if isinstance(payload, tuple) and len(payload) >= 2 and isinstance(payload[1], (bytes, bytearray)):
        return email.message_from_bytes(bytes(payload[1]))
    return None


def message_identifier(msg: Message, uid: bytes) -> str:
    mid = (msg.get("Message-ID") or "").strip()
    return mid or f"imap-uid:{uid.decode('ascii', 'replace')}"


def dump_unrouted(msg: Message, uid: bytes) -> Path:
    UNROUTED_DUMP_DIR.mkdir(parents=True, exist_ok=True)
    sender = (msg.get("From") or "unknown").replace("/", "_").replace("\\", "_")
    safe_sender = "".join(ch for ch in sender if ch.isalnum() or ch in "._-@")[:60]
    target = UNROUTED_DUMP_DIR / f"unrouted_{safe_sender}_{uid.decode('ascii', 'replace')}.eml"
    target.write_bytes(msg.as_bytes())
    return target


def listing_input_from_parsed(parsed, fallback_subject: str) -> ListingInput:
    return ListingInput(
        url=parsed.url,
        source=parsed.source,
        title=parsed.title or fallback_subject or None,
        rent_chf=parsed.rent_chf,
        city=parsed.city,
        move_in=parsed.move_in,
        contact_name=None,
        contact_email=None,
        raw_text=parsed.raw_text,
        commute_minutes=None,
    )


def process_message(
    msg: Message,
    uid: bytes,
    conn: sqlite3.Connection,
    stats: IngestStats,
    *,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Returns the number of listings created or updated from this message."""
    from_header = msg.get("From") or ""
    parser = parser_for_sender(from_header)
    if parser is None:
        stats.unrouted += 1
        if not dry_run:
            dumped = dump_unrouted(msg, uid)
            if verbose:
                print(f"  unrouted: from={from_header!r} dumped={dumped}")
        elif verbose:
            print(f"  unrouted: from={from_header!r} (dry-run, not dumped)")
        return 0

    try:
        parsed_listings = parser.parse(msg)
    except Exception as exc:  # noqa: BLE001 - keep the loop going
        stats.errors.append(f"parse-error from={from_header!r}: {exc!r}")
        if verbose:
            print(f"  parse error: {exc!r}")
        return 0

    stats.routed += 1
    subject = (msg.get("Subject") or "").strip()
    touched = 0
    for parsed in parsed_listings:
        stats.listings_seen += 1
        listing = listing_input_from_parsed(parsed, fallback_subject=subject)
        scored = score_listing(normalize_listing(listing))
        if dry_run:
            stats.listings_skipped += 1
            if verbose:
                print(
                    f"  [dry-run] would upsert {parser.source} {parsed.url} "
                    f"-> decision={scored.decision} score={scored.priority_score}"
                )
            continue
        listing_id, created = upsert_listing(conn, scored)
        if created:
            stats.listings_created += 1
        else:
            stats.listings_updated += 1
        touched += 1
        if verbose:
            print(
                f"  {'created' if created else 'updated'} #{listing_id}: "
                f"{parser.source} {scored.decision} score={scored.priority_score} "
                f"{parsed.url}"
            )
    return touched


def iter_messages(
    client: imaplib.IMAP4_SSL,
    uids: list[bytes],
) -> Iterator[tuple[bytes, Message]]:
    for uid in uids:
        msg = fetch_message(client, uid)
        if msg is not None:
            yield uid, msg


def sync_email_alerts(args: argparse.Namespace) -> IngestStats:
    _load_env()
    address, password = imap_credentials()
    stats = IngestStats()

    with closing(connect(args.db)) as conn:
        init_db(conn)
        init_email_seen_table(conn)

        client = connect_imap(IMAP_HOST, address, password, args.mailbox)
        try:
            since = imap_date(args.days)
            if args.verbose:
                print(f"Searching {args.mailbox!r} for portal alerts since {since}")
            uids = search_alerts(client, since, args.limit)
            if args.verbose:
                print(f"Found {len(uids)} candidate message(s)")
            for uid, msg in iter_messages(client, uids):
                stats.fetched += 1
                identifier = message_identifier(msg, uid)
                if not args.reprocess and already_seen(conn, identifier):
                    if args.verbose:
                        print(f"  skip: already processed message_id={identifier}")
                    continue
                touched = process_message(
                    msg,
                    uid,
                    conn,
                    stats,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                )
                if not args.dry_run:
                    parser = parser_for_sender(msg.get("From") or "")
                    source_label = parser.source if parser else "unknown"
                    record_seen(conn, identifier, source_label, touched)
            if not args.dry_run:
                conn.commit()
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest portal saved-search alerts from Gmail (read-only IMAP)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite tracker path.")
    parser.add_argument("--mailbox", default=DEFAULT_MAILBOX, help="IMAP mailbox to scan.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Look back N days.")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT, help="Max messages per run.")
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Ignore the email_alert_seen table and reparse every match.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + score without writing to SQLite or the seen-table.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.days < 1:
        raise SystemExit("--days must be at least 1.")
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1.")
    stats = sync_email_alerts(args)
    print(
        "Email alert ingest complete: "
        f"fetched={stats.fetched}, routed={stats.routed}, unrouted={stats.unrouted}, "
        f"listings_seen={stats.listings_seen}, created={stats.listings_created}, "
        f"updated={stats.listings_updated}, skipped={stats.listings_skipped}"
    )
    if stats.errors:
        print(f"Errors: {len(stats.errors)} (first 5):")
        for err in stats.errors[:5]:
            print(f"  - {err}")
    if args.dry_run:
        print("Dry run: tracker and seen-table were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
