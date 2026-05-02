"""Audit free-aggregator yield.

Reads .tmp/seen_jobs.json and prints, per source, how many jobs were
first seen in the last N days. Use to decide whether to keep or drop a
free aggregator (talent.com, jooble, whatjobs) from scrape_jobs.py.

Usage:
    python execution/audit_aggregators.py [--days 30]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEEN_JOBS_FILE = Path(__file__).resolve().parent.parent / ".tmp" / "seen_jobs.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Window in days (default 30)")
    args = parser.parse_args()

    if not SEEN_JOBS_FILE.exists():
        print(f"No seen-jobs file at {SEEN_JOBS_FILE}")
        return

    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        seen = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    counts: Counter[str] = Counter()
    unknown_recent = 0
    legacy = 0

    for v in seen.values():
        if isinstance(v, str):
            legacy += 1
            ts = v
            src = None
        elif isinstance(v, dict):
            ts = v.get("first_seen", "")
            src = v.get("source")
        else:
            continue

        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if t < cutoff:
            continue
        if not src:
            unknown_recent += 1
            continue
        counts[src] += 1

    total = sum(counts.values())
    print(f"Window: last {args.days} days  (total tagged entries: {total})")
    print(f"{'source':<24} {'count':>6} {'share':>8}")
    print("-" * 42)
    for src, n in counts.most_common():
        share = (n / total * 100) if total else 0.0
        print(f"{src:<24} {n:>6} {share:>7.1f}%")
    print("-" * 42)
    print(f"{'(untagged, recent)':<24} {unknown_recent:>6}")
    print(f"{'(legacy entries)':<24} {legacy:>6}    pre-source-tracking writes")
    print()
    print("Drop candidates: any source <5% over a meaningful window.")


if __name__ == "__main__":
    main()
