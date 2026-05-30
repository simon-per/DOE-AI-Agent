#!/usr/bin/env python3
"""Live public-transport (oeV) commute scoring via transport.opendata.ch.

Companion to commute_scoring.py (e-bike via OpenRouteService). For Lucerne-core
listings the train beats the bike to Root D4 (~15 min vs ~35 min), so scoring
that only ever looked at cycling under-ranked them. This module adds the transit
leg.

Design mirrors commute_scoring.py deliberately:
- stdlib urllib only, no new deps
- graceful fallback: any failure returns None and the caller keeps its static
  estimate, so scoring never breaks when the API is down
- **reuses** commute_scoring's SQLite cache (data/commute_cache.sqlite) and its
  init_cache / cache_get / cache_put / _http_json helpers — no duplicate schema.
  Transit rows are namespaced by a work-key suffix so they never collide with
  the e-bike row for the same origin.

Unlike the ORS path, transport.opendata.ch needs **no API key** — transit
scoring works out of the box.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution import commute_scoring as cs  # noqa: E402


TRANSPORT_BASE_URL = "https://transport.opendata.ch"
CONNECTIONS_PATH = "/v1/connections"

# The PHENOGY office sits at the "Root D4" S-Bahn stop, which the transport API
# resolves directly — a better transit anchor than the street address ORS wants.
WORK_TRANSIT_DESTINATION = "Root D4"

# Anchor the query to a typical commute departure so we score the morning
# timetable rather than a sparse late-night one.
MORNING_DEPARTURE = "08:00"

DEFAULT_CONNECTION_LIMIT = 3
TRANSIT_MODE = "oeV (live)"
# The connections API returns stop-to-stop time only. Real door-to-door also
# needs walk-to-origin-stop + wait + walk-from-Root-D4-stop-to-office. Without
# this, near towns get absurd values (Buchrain "2 min") that both mislead drafts
# and unfairly beat the e-bike leg, which is already true door-to-door.
TRANSIT_ACCESS_BUFFER_MIN = 12
# Keeps transit rows distinct from e-bike rows in the shared commute_cache,
# whose primary key is (origin_key, work_key).
_TRANSIT_WORK_SUFFIX = " #oev"

# "00d00:43:00" -> 0 days, 0h, 43m, 0s. The leading "<n>d" is always present in
# the API response but treated as optional for resilience.
_DURATION_RE = re.compile(r"(?:(\d+)d)?(\d{1,2}):(\d{2}):(\d{2})")


def is_enabled() -> bool:
    """Transit needs no API key, so it is ON by default. Set
    TRANSPORT_OPENDATA_ENABLED=0 (or false/no/off) to force the static
    fallback — the test suite does this to stay offline and deterministic."""
    cs._load_env_chain()
    value = os.getenv("TRANSPORT_OPENDATA_ENABLED", "1").strip().lower()
    return value not in ("0", "false", "no", "off", "")


def parse_duration_to_minutes(duration: str) -> int | None:
    """Convert a transport.opendata.ch duration string to whole minutes."""
    if not duration:
        return None
    match = _DURATION_RE.fullmatch(duration.strip())
    if not match:
        return None
    days = int(match.group(1) or 0)
    hours = int(match.group(2))
    minutes = int(match.group(3))
    seconds = int(match.group(4))
    total = days * 24 * 60 + hours * 60 + minutes + (1 if seconds >= 30 else 0)
    return max(1, total)


def _next_weekday_date(today: date | None = None) -> str:
    """Next Tuesday (today if it's already Tuesday) as YYYY-MM-DD.

    transport.opendata.ch defaults `date` to the run day, so a weekend/holiday
    cron would score that day's sparse timetable and pin it in the 30-day cache.
    Anchoring to a mid-week weekday keeps the cached duration representative
    regardless of when the run happens. Tuesday avoids Monday holiday spillover."""
    day = today or date.today()
    days_ahead = (1 - day.weekday()) % 7  # Monday=0 … Tuesday=1
    return (day + timedelta(days=days_ahead)).isoformat()


def _query_transit_minutes(
    from_addr: str,
    to_addr: str,
    *,
    base_url: str = TRANSPORT_BASE_URL,
    limit: int = DEFAULT_CONNECTION_LIMIT,
) -> int | None:
    """Fastest door-to-door connection in minutes, or None on any failure."""
    params = urllib.parse.urlencode({
        "from": from_addr,
        "to": to_addr,
        "limit": str(limit),
        "date": _next_weekday_date(),
        "time": MORNING_DEPARTURE,
        # Trim the payload — we only need each connection's duration.
        "fields[]": "connections/duration",
    }, doseq=True)
    # Reuse commute_scoring's HTTP helper: same retry/timeout/observability,
    # retries only on 429 + 5xx, fail-silent on 4xx.
    body = cs._http_json(f"{base_url}{CONNECTIONS_PATH}?{params}")
    if not body:
        return None
    connections = body.get("connections") or []
    durations = [
        m for c in connections
        if (m := parse_duration_to_minutes(c.get("duration") or "")) is not None
    ]
    if not durations:
        # Distinguish "no routes" (quietly fall back) from "API shape changed"
        # (connections present but no parseable duration) so the latter is visible.
        if connections:
            print(
                f"[transit_scoring] {len(connections)} connection(s) but no parseable "
                f"duration for {from_addr!r} -> {to_addr!r} (API shape changed?)",
                file=sys.stderr,
            )
        return None
    return min(durations)


def live_transit_minutes(
    address_text: str,
    *,
    destination: str | None = None,
    cache_path: Path = cs.DEFAULT_CACHE_PATH,
) -> cs.RouteResult | None:
    """Live oeV commute to Root D4, or None if the live path is unavailable.

    Callers must fall back to a static estimate. Results cache for the same TTL
    as the e-bike path, in the same SQLite file, under a namespaced work key."""
    if not is_enabled():
        return None
    origin_text = (address_text or "").strip()
    if not origin_text:
        return None
    dest_text = (destination or WORK_TRANSIT_DESTINATION).strip()
    origin_key = cs.normalize_key(origin_text)
    work_key = cs.normalize_key(dest_text) + _TRANSIT_WORK_SUFFIX

    with closing(cs.init_cache(cache_path)) as conn:
        cached = cs.cache_get(conn, origin_key, work_key)
        if cached:
            return cached
        raw_minutes = _query_transit_minutes(origin_text, dest_text)
        if raw_minutes is None:
            return None
        # Add the access/egress buffer so the cached value is realistic
        # door-to-door and comparable to the e-bike leg.
        minutes = raw_minutes + TRANSIT_ACCESS_BUFFER_MIN
        result = cs.RouteResult(minutes=minutes, mode=TRANSIT_MODE, distance_km=None)
        cs.cache_put(conn, origin_key, work_key, result)
        return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Query a live oeV commute via transport.opendata.ch.",
    )
    parser.add_argument("address", nargs="+", help="Listing address / city (free-form).")
    parser.add_argument("--to", default=WORK_TRANSIT_DESTINATION, help="Destination stop.")
    parser.add_argument("--cache", type=Path, default=cs.DEFAULT_CACHE_PATH)
    args = parser.parse_args(argv)

    address = " ".join(args.address)
    result = live_transit_minutes(address, destination=args.to, cache_path=args.cache)
    if result is None:
        print(f"No transit result for {address!r} -> {args.to!r}.")
        return 1
    print(f"{address!r} -> {args.to!r}: {result.minutes} min via {result.mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
