#!/usr/bin/env python3
"""Live commute scoring against PHENOGY (Platz 4, 6039 Root D4) via OpenRouteService.

The existing apartment_pipeline uses a hand-curated city → minutes lookup
(COMMUTE_ESTIMATES). That table is fine for known suburbs but degrades to
"unknown" the moment a listing's city falls outside Root D4 / Lucerne core.
This module replaces it with live routing for any listing whose address
text can be geocoded — and caches the result so we don't burn ORS quota on
the same suburb twice.

Quietly opt-in:
- Without `OPENROUTESERVICE_API_KEY` in env, `live_commute_minutes()` returns
  None and the pipeline falls back to its static table. Scoring still works.
- All HTTP calls go through stdlib urllib. No new deps.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_PATH = BASE_DIR / "data" / "commute_cache.sqlite"

# Pre-geocoded coordinates for PHENOGY AG, Platz 4, 6039 Root D4. Saves one
# ORS geocode per cold cache start, and the value is stable.
WORK_DEFAULT_ADDRESS = os.getenv("WORK_ADDRESS", "Platz 4, 6039 Root D4, Switzerland")
WORK_DEFAULT_LATLNG = (47.1306, 8.4226)

ORS_BASE_URL = "https://api.openrouteservice.org"
GEOCODE_PATH = "/geocode/search"
ROUTE_EBIKE_PATH = "/v2/directions/cycling-electric"
USER_AGENT = "apartment-search-agent/0.1 (+commute scoring; low volume)"

CACHE_TTL_DAYS = 30
DEFAULT_TIMEOUT = 12
# Total HTTP attempts (1 retry). transport.opendata.ch / ORS only get retried
# on 429 + 5xx; permanent 4xx returns immediately.
DEFAULT_MAX_ATTEMPTS = 2

SWITZERLAND_BBOX = "5.95,45.81,10.49,47.81"


@dataclass(frozen=True)
class RouteResult:
    minutes: int
    mode: str
    distance_km: float | None


def _load_env_chain() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")


def api_key() -> str | None:
    _load_env_chain()
    key = os.environ.get("OPENROUTESERVICE_API_KEY", "").strip()
    return key or None


def is_enabled() -> bool:
    return api_key() is not None


def normalize_key(text: str) -> str:
    return " ".join((text or "").lower().split())


def init_cache(path: Path = DEFAULT_CACHE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    # This cache file is shared with transit_scoring.py — WAL + a busy timeout
    # let the e-bike and OeV writers touch it concurrently without "database
    # is locked" errors.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS commute_cache (
            origin_key TEXT NOT NULL,
            work_key TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            mode TEXT NOT NULL,
            distance_km REAL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (origin_key, work_key)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address_key TEXT PRIMARY KEY,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            cached_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _cache_fresh(computed_at: str, ttl_days: int) -> bool:
    try:
        when = datetime.fromisoformat(computed_at)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return datetime.now(UTC) - when < timedelta(days=ttl_days)


def cache_get(
    conn: sqlite3.Connection,
    origin_key: str,
    work_key: str,
    ttl_days: int = CACHE_TTL_DAYS,
) -> RouteResult | None:
    row = conn.execute(
        "SELECT minutes, mode, distance_km, computed_at FROM commute_cache "
        "WHERE origin_key = ? AND work_key = ?",
        (origin_key, work_key),
    ).fetchone()
    if not row or not _cache_fresh(row[3], ttl_days):
        return None
    return RouteResult(minutes=int(row[0]), mode=str(row[1]), distance_km=row[2])


def cache_put(
    conn: sqlite3.Connection,
    origin_key: str,
    work_key: str,
    result: RouteResult,
) -> None:
    conn.execute(
        """
        INSERT INTO commute_cache (origin_key, work_key, minutes, mode, distance_km, computed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(origin_key, work_key) DO UPDATE SET
            minutes      = excluded.minutes,
            mode         = excluded.mode,
            distance_km  = excluded.distance_km,
            computed_at  = excluded.computed_at
        """,
        (origin_key, work_key, result.minutes, result.mode, result.distance_km,
         datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any] | None:
    method = "POST" if data is not None else "GET"
    final_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        final_headers["Content-Type"] = "application/json"
    if headers:
        final_headers.update(headers)
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        req = urllib.request.Request(url, data=data, method=method, headers=final_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx outside of rate-limit is permanent; don't retry pointlessly.
            if exc.code not in (429, 500, 502, 503, 504):
                return None
            last_exc = exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
        if attempt < max_attempts - 1:
            time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        # Drop any query string so a legacy api_key in the URL never lands in logs.
        print(
            f"[commute_scoring] giving up on {url.split('?', 1)[0]} "
            f"after {max_attempts} attempt(s): {last_exc!r}",
            file=sys.stderr,
        )
    return None


def geocode(
    address: str,
    conn: sqlite3.Connection,
    *,
    base_url: str = ORS_BASE_URL,
    boundary_bbox: str = SWITZERLAND_BBOX,
) -> tuple[float, float] | None:
    key = api_key()
    if not key:
        return None
    cache_key = normalize_key(address)
    if not cache_key:
        return None
    row = conn.execute(
        "SELECT lat, lng FROM geocode_cache WHERE address_key = ?",
        (cache_key,),
    ).fetchone()
    if row:
        return float(row[0]), float(row[1])

    params = urllib.parse.urlencode({
        "text": address,
        "boundary.rect.min_lon": boundary_bbox.split(",")[0],
        "boundary.rect.min_lat": boundary_bbox.split(",")[1],
        "boundary.rect.max_lon": boundary_bbox.split(",")[2],
        "boundary.rect.max_lat": boundary_bbox.split(",")[3],
        "size": "1",
    })
    # Key travels in the Authorization header (same as route_ebike) so it never
    # lands in URL query logs.
    body = _http_json(
        f"{base_url}{GEOCODE_PATH}?{params}", headers={"Authorization": key}
    )
    if not body:
        return None
    features = body.get("features") or []
    if not features:
        return None
    coords = features[0].get("geometry", {}).get("coordinates") or []
    if len(coords) < 2:
        return None
    lng, lat = float(coords[0]), float(coords[1])
    # ORS treats boundary.rect as a soft hint, so a typo'd address can still
    # resolve to a far-away namesake. Hard-reject anything outside the box to
    # keep junk coordinates (and junk routes) out of the cache.
    min_lon, min_lat, max_lon, max_lat = (float(x) for x in boundary_bbox.split(","))
    if not (min_lon <= lng <= max_lon and min_lat <= lat <= max_lat):
        return None
    conn.execute(
        "INSERT OR REPLACE INTO geocode_cache (address_key, lat, lng, cached_at) "
        "VALUES (?, ?, ?, ?)",
        (cache_key, lat, lng, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return lat, lng


def route_ebike(
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    base_url: str = ORS_BASE_URL,
) -> RouteResult | None:
    key = api_key()
    if not key:
        return None
    payload = json.dumps({
        # ORS expects [lon, lat] pairs.
        "coordinates": [
            [origin[1], origin[0]],
            [destination[1], destination[0]],
        ],
        "instructions": False,
    }).encode("utf-8")
    body = _http_json(
        f"{base_url}{ROUTE_EBIKE_PATH}",
        headers={"Authorization": key},
        data=payload,
    )
    if not body:
        return None
    routes = body.get("routes") or []
    if not routes:
        return None
    summary = routes[0].get("summary") or {}
    duration_s = summary.get("duration")
    distance_m = summary.get("distance")
    if duration_s is None:
        return None
    minutes = max(1, int(round(float(duration_s) / 60.0)))
    distance_km = round(float(distance_m) / 1000.0, 2) if distance_m is not None else None
    return RouteResult(minutes=minutes, mode="e-bike (live)", distance_km=distance_km)


def live_commute_minutes(
    address_text: str,
    *,
    work_address: str | None = None,
    work_coords: tuple[float, float] | None = None,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> RouteResult | None:
    """Returns a live e-bike commute to work, or None if the live path is
    unavailable for any reason. Callers must fall back to a static estimate."""
    if not is_enabled():
        return None
    origin_text = (address_text or "").strip()
    if not origin_text:
        return None
    work_text = (work_address or WORK_DEFAULT_ADDRESS).strip()
    work_key = normalize_key(work_text)
    origin_key = normalize_key(origin_text)

    with closing(init_cache(cache_path)) as conn:
        cached = cache_get(conn, origin_key, work_key)
        if cached:
            return cached
        dest = work_coords or WORK_DEFAULT_LATLNG
        if work_coords is None and work_text != WORK_DEFAULT_ADDRESS:
            geocoded_dest = geocode(work_text, conn)
            if geocoded_dest:
                dest = geocoded_dest
        origin_coords = geocode(origin_text, conn)
        if not origin_coords:
            return None
        result = route_ebike(origin_coords, dest)
        if result is None:
            return None
        cache_put(conn, origin_key, work_key, result)
        return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Query a live ORS commute to PHENOGY.")
    parser.add_argument("address", nargs="+", help="Listing address text (free-form).")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_PATH)
    args = parser.parse_args(argv)

    address = " ".join(args.address)
    result = live_commute_minutes(address, cache_path=args.cache)
    if result is None:
        print(f"No live result for {address!r}. API key set? {is_enabled()}")
        return 1
    print(f"{address!r}: {result.minutes} min via {result.mode}"
          + (f" ({result.distance_km} km)" if result.distance_km else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
