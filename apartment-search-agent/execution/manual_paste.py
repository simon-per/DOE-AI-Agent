#!/usr/bin/env python3
"""Batch-ingest manually pasted listings from any portal.

The existing `apartment_pipeline.py ingest` handles one listing at a time via
CLI flags. This wrapper accepts a *batch* of listings in a forgiving text
format so you can paste multiple Facebook-group, Anibis, Comparis, or
private listings in one go — one file or stdin chunk, zero flags per row.

Block format (one block per listing, blocks separated by lines of `===`):

    url: https://www.facebook.com/groups/12345/posts/67890
    source: facebook
    contact_email: anna@example.ch

    WG-Zimmer in Luzern, CHF 750, ab 1.8.
    Möbliert, Anmeldung möglich. Bei Interesse bitte melden.
    ===
    url: https://www.anibis.ch/de/d/wohnung-mieten-luzern-12345

    Schöne 2-Zimmer Wohnung in Luzern, CHF 1200, frei ab 16.07.

Header lines (any subset) precede a blank line; everything after the blank
line is the raw body. Source defaults to `detect_source()` over the URL.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DB_PATH,
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    print_listing_summary,
    score_listing,
    upsert_listing,
)

BLOCK_SEPARATOR = "==="
KNOWN_HEADERS = {
    "url",
    "source",
    "title",
    "rent",
    "rent_chf",
    "city",
    "move_in",
    "contact_name",
    "contact_email",
    "commute_minutes",
}


@dataclass
class BatchStats:
    blocks_seen: int = 0
    blocks_skipped: int = 0
    created: int = 0
    updated: int = 0


def split_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.strip() == BLOCK_SEPARATOR:
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(raw_line)
    if current:
        tail = "\n".join(current).strip()
        if tail:
            blocks.append(tail)
    return [b for b in blocks if b.strip()]


def parse_block(block: str) -> ListingInput | None:
    """Convert one block of header + body text into a ListingInput.

    Returns None for blocks that have no URL — those can't be tracked or
    deduped reliably, so they're skipped instead of guessing.
    """
    lines = block.splitlines()
    headers: dict[str, str] = {}
    body_start = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "":
            body_start = idx + 1
            break
        if ":" not in stripped:
            # First non-header line means the body started already.
            body_start = idx
            break
        key, _, value = stripped.partition(":")
        key_norm = key.strip().lower()
        if key_norm not in KNOWN_HEADERS:
            body_start = idx
            break
        headers[key_norm] = value.strip()
    else:
        body_start = len(lines)

    body = "\n".join(lines[body_start:]).strip()
    url = headers.get("url")
    if not url:
        return None
    # A `url:` value that isn't an http(s) URL can't be deduped or opened —
    # skip it instead of storing a bogus canonical key.
    if not url.lower().startswith(("http://", "https://")):
        return None

    rent_raw = headers.get("rent") or headers.get("rent_chf")
    rent_chf = None
    if rent_raw:
        try:
            rent_chf = int(rent_raw.replace("CHF", "").replace("'", "").replace(",", "").strip())
        except ValueError:
            rent_chf = None

    commute_minutes = None
    commute_raw = headers.get("commute_minutes")
    if commute_raw:
        try:
            commute_minutes = int(commute_raw)
        except ValueError:
            commute_minutes = None

    return ListingInput(
        url=url,
        source=headers.get("source"),
        title=headers.get("title"),
        rent_chf=rent_chf,
        city=headers.get("city"),
        move_in=headers.get("move_in"),
        contact_name=headers.get("contact_name"),
        contact_email=headers.get("contact_email"),
        raw_text=body or url,
        commute_minutes=commute_minutes,
    )


def ingest_blocks(blocks: Iterable[str], args: argparse.Namespace) -> BatchStats:
    stats = BatchStats()
    with closing(connect(args.db)) as conn:
        init_db(conn)
        for block in blocks:
            stats.blocks_seen += 1
            listing = parse_block(block)
            if listing is None:
                stats.blocks_skipped += 1
                if args.verbose:
                    print(f"  skip: block {stats.blocks_seen} has no `url:` header")
                continue
            scored = score_listing(normalize_listing(listing))
            if args.dry_run:
                if args.verbose:
                    print(
                        f"  [dry-run] would upsert {listing.url} -> "
                        f"decision={scored.decision} score={scored.priority_score}"
                    )
                continue
            listing_id, created = upsert_listing(conn, scored)
            if created:
                stats.created += 1
            else:
                stats.updated += 1
            if args.verbose:
                print_listing_summary(listing_id, created, scored)
            else:
                action = "created" if created else "updated"
                print(
                    f"{action} #{listing_id} {scored.decision} score={scored.priority_score} "
                    f"{listing.url}"
                )
    return stats


def read_source(args: argparse.Namespace) -> str:
    if args.file:
        # utf-8-sig transparently strips a leading BOM if the paste came from
        # Notepad/Excel; plain utf-8 would leave it stuck on the first header.
        return args.file.read_text(encoding="utf-8-sig")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit(
        "No input — pass --file <path> or pipe blocks on stdin. "
        f"Block format documented in {__file__}."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch-ingest manually pasted listings from any portal.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite tracker path.")
    parser.add_argument("--file", type=Path, help="Read blocks from this file. Defaults to stdin.")
    parser.add_argument("--dry-run", action="store_true", help="Parse + score without writing to SQLite.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    text = read_source(args)
    blocks = split_blocks(text)
    if not blocks:
        print("No blocks found in input.")
        return 0
    stats = ingest_blocks(blocks, args)
    print(
        "Manual paste ingest complete: "
        f"blocks={stats.blocks_seen}, skipped={stats.blocks_skipped}, "
        f"created={stats.created}, updated={stats.updated}"
    )
    if args.dry_run:
        print("Dry run: tracker was not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
