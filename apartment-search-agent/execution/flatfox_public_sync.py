#!/usr/bin/env python3
"""Sync Flatfox public listings into the local apartment tracker.

Flatfox documents `/api/v1/public-listing/` as a public, unauthenticated
endpoint for currently published listings. This script uses only that endpoint,
filters locally for Simon's Root D4 search, and writes matching listings into
the existing SQLite tracker via `apartment_pipeline.py`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DB_PATH,
    MAX_RENT_CHF,
    ListingInput,
    connect,
    init_db,
    normalize_for_match,
    normalize_listing,
    score_listing,
    upsert_listing,
)


FLATFOX_BASE_URL = "https://flatfox.ch"
PUBLIC_LISTING_PATH = "/api/v1/public-listing/"
USER_AGENT = "apartment-search-agent/0.1 (+manual Simon apartment search)"
TRACKER_SOURCE = "flatfox.ch"
MONTHLY_PRICE_UNIT = "monthly"
TRANSIENT_HTTP_STATUS = {429, 500, 502, 503, 504}

# Approximate city-centre corridor from Luzern to Rotkreuz. Filtering is still
# local because Flatfox's documented public-listing endpoint does not expose
# direct price/category/geo query parameters.
AREA_PRESETS: dict[str, tuple[tuple[float, float], tuple[float, float]]] = {
    "luzern-rotkreuz": ((47.0502, 8.3093), (47.1416, 8.4314)),
}
DEFAULT_AREA = "luzern-rotkreuz"
DEFAULT_CORRIDOR_KM = 5.0

DEFAULT_TARGET_CITIES = [
    "Root D4",
    "Root",
    "Gisikon-Root",
    "Gisikon",
    "Honau",
    "Dierikon",
    "Buchrain",
    "Ebikon",
    "Rotkreuz",
    "Luzern",
    "Lucerne",
    "Perlen",
    "Inwil",
    "Adligenswil",
    "Emmen",
    "Emmenbruecke",
    "Emmenbrucke",
]

TARGET_OBJECT_CATEGORIES = {"APARTMENT", "SHARED"}
TARGET_OBJECT_TYPES = {
    "APARTMENT",
    "STUDIO",
    "SINGLE_ROOM",
    "FURNISHED_FLAT",
    "SHARED_FLAT",
    "BACHELOR_FLAT",
    "ATTIC",
    "ATTIC_FLAT",
    "DUPLEX",
    "ROOF_FLAT",
    "TERRACE_FLAT",
}


class FlatfoxApiError(RuntimeError):
    """Raised when the Flatfox public API returns an unexpected response."""


class FlatfoxRateLimitError(FlatfoxApiError):
    """Raised when Flatfox asks the client to slow down."""


@dataclass(frozen=True)
class SyncStats:
    fetched: int = 0
    matched: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    pages: int = 0

    def add(self, **changes: int) -> "SyncStats":
        values = self.__dict__.copy()
        for key, value in changes.items():
            values[key] += value
        return SyncStats(**values)


def full_url(base_url: str, path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))


def build_public_listing_url(
    base_url: str,
    *,
    limit: int,
    offset: int,
    status: str,
    selection: int | None = None,
    pk: list[int] | None = None,
    expand: list[str] | None = None,
    include: list[str] | None = None,
) -> str:
    query: list[tuple[str, str | int]] = [
        ("limit", limit),
        ("offset", offset),
        ("status", status),
    ]
    if selection is not None:
        query.append(("selection", selection))
    for value in pk or []:
        query.append(("pk", value))
    for value in expand or []:
        query.append(("expand", value))
    for value in include or []:
        query.append(("include", value))
    return full_url(base_url, PUBLIC_LISTING_PATH) + "?" + urllib.parse.urlencode(query, doseq=True)


class FlatfoxPublicClient:
    def __init__(
        self,
        base_url: str = FLATFOX_BASE_URL,
        timeout_seconds: int = 30,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def get_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(min(2**attempt, 10))
            try:
                return self._open_json(request, url)
            except FlatfoxRateLimitError:
                raise
            except FlatfoxApiError as exc:
                last_error = exc
                if not any(f"HTTP {status}" in str(exc) for status in TRANSIENT_HTTP_STATUS):
                    raise
        raise FlatfoxApiError(f"Flatfox request failed after retries: {last_error}")

    def _open_json(self, request: urllib.request.Request, url: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise FlatfoxApiError(f"Flatfox returned HTTP {response.status} for {url}")
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise FlatfoxApiError(f"Flatfox returned non-object JSON for {url}")
                return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                suffix = f" Retry after {retry_after} seconds." if retry_after else ""
                raise FlatfoxRateLimitError(
                    f"Flatfox rate-limited the request for {url}.{suffix}"
                ) from exc
            raise FlatfoxApiError(f"Flatfox returned HTTP {exc.code} for {url}: {detail}") from exc
        except json.JSONDecodeError as exc:
            raise FlatfoxApiError(f"Flatfox returned invalid JSON for {url}: {exc}") from exc
        except urllib.error.URLError as exc:
            raise FlatfoxApiError(f"Could not reach Flatfox public API: {exc}") from exc


def rent_from_public_listing(item: dict[str, Any]) -> int | None:
    if str(item.get("price_unit") or "").lower() != MONTHLY_PRICE_UNIT:
        return None
    for field in ("rent_gross", "price_display", "rent_net"):
        value = item.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return None


def target_city_names(cities: list[str]) -> set[str]:
    return {normalize_for_match(city) for city in cities if city.strip()}


def is_target_city(item: dict[str, Any], cities: set[str]) -> bool:
    city = normalize_for_match(str(item.get("city") or ""))
    if not city:
        return False
    return city in cities


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def point_to_segment_distance_km(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    lat, lon = point
    start_lat, start_lon = start
    end_lat, end_lon = end
    mean_lat_rad = math.radians((lat + start_lat + end_lat) / 3.0)

    def project(coord: tuple[float, float]) -> tuple[float, float]:
        coord_lat, coord_lon = coord
        return (
            coord_lon * 111.320 * math.cos(mean_lat_rad),
            coord_lat * 110.574,
        )

    px, py = project(point)
    ax, ay = project(start)
    bx, by = project(end)
    dx = bx - ax
    dy = by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)

    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return math.hypot(px - closest_x, py - closest_y)


def is_inside_bbox(
    point: tuple[float, float],
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if bbox is None:
        return False
    south, west, north, east = bbox
    lat, lon = point
    return south <= lat <= north and west <= lon <= east


def is_inside_corridor(
    point: tuple[float, float],
    corridor: tuple[tuple[float, float], tuple[float, float]] | None,
    corridor_km: float,
) -> bool:
    if corridor is None:
        return False
    return point_to_segment_distance_km(point, corridor[0], corridor[1]) <= corridor_km


def is_target_area(
    item: dict[str, Any],
    *,
    cities: set[str],
    corridor: tuple[tuple[float, float], tuple[float, float]] | None,
    corridor_km: float,
    bbox: tuple[float, float, float, float] | None,
) -> bool:
    if is_target_city(item, cities):
        return True

    lat = as_float(item.get("latitude"))
    lon = as_float(item.get("longitude"))
    if lat is None or lon is None:
        return False
    point = (lat, lon)
    return is_inside_bbox(point, bbox) or is_inside_corridor(point, corridor, corridor_km)


def is_target_object(item: dict[str, Any]) -> bool:
    category = str(item.get("object_category") or "").upper()
    object_type = str(item.get("object_type") or "").upper()
    return category in TARGET_OBJECT_CATEGORIES or object_type in TARGET_OBJECT_TYPES


def should_ingest_public_listing(
    item: dict[str, Any],
    *,
    cities: set[str],
    corridor: tuple[tuple[float, float], tuple[float, float]] | None,
    corridor_km: float,
    bbox: tuple[float, float, float, float] | None,
    max_rent: int,
    include_over_budget: bool,
    include_unknown_rent: bool,
) -> tuple[bool, str]:
    if str(item.get("offer_type") or "").upper() != "RENT":
        return False, "not rent"
    if str(item.get("status") or "").lower() != "act":
        return False, "not active"
    if not is_target_object(item):
        return False, "not target object type"
    if not is_target_area(
        item,
        cities=cities,
        corridor=corridor,
        corridor_km=corridor_km,
        bbox=bbox,
    ):
        return False, "outside target area"

    rent = rent_from_public_listing(item)
    if str(item.get("price_unit") or "").lower() != MONTHLY_PRICE_UNIT:
        return False, "non-monthly price unit"
    if rent is None and not include_unknown_rent:
        return False, "unknown rent"
    if rent is not None and rent > max_rent and not include_over_budget:
        return False, "over budget"
    return True, "matched"


def attribute_name(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("name", "label", "value", "type"):
            if value.get(key):
                return str(value[key])
    return str(value)


def raw_text_from_public_listing(item: dict[str, Any]) -> str:
    attributes = item.get("attributes") or []
    if isinstance(attributes, list):
        attributes_text = ", ".join(attribute_name(value) for value in attributes)
    else:
        attributes_text = str(attributes)

    lines = [
        str(item.get("public_title") or item.get("short_title") or ""),
        str(item.get("description_title") or ""),
        str(item.get("description") or ""),
        f"Flatfox pk: {item.get('pk')}",
        f"Object category: {item.get('object_category')}",
        f"Object type: {item.get('object_type')}",
        f"Address: {item.get('public_address') or ''}",
        f"City: {item.get('city') or ''}",
        f"Zipcode: {item.get('zipcode') or ''}",
        f"Rent gross: {item.get('rent_gross') or ''}",
        f"Price display: {item.get('price_display') or ''}",
        f"Moving date: {item.get('moving_date') or ''}",
        f"Temporary: {item.get('is_temporary')}",
        f"Furnished: {item.get('is_furnished')}",
        f"Selling furniture: {item.get('is_selling_furniture')}",
        f"Attributes: {attributes_text}",
    ]
    if item.get("is_furnished"):
        lines.append("moebliert")
    if item.get("is_temporary"):
        lines.append("befristet")
    if item.get("is_selling_furniture"):
        lines.append("moebel abkaufen")
    return "\n".join(line for line in lines if line.strip())


def listing_input_from_public_listing(item: dict[str, Any], base_url: str) -> ListingInput:
    title = item.get("public_title") or item.get("short_title") or item.get("rent_title")
    return ListingInput(
        url=full_url(base_url, str(item.get("url") or item.get("short_url") or "")),
        source=TRACKER_SOURCE,
        title=str(title or f"Flatfox listing {item.get('pk')}"),
        rent_chf=rent_from_public_listing(item),
        city=str(item.get("city") or ""),
        move_in=str(item.get("moving_date") or ""),
        contact_name=None,
        contact_email=None,
        raw_text=raw_text_from_public_listing(item),
        commute_minutes=None,
    )


def iter_public_listing_pages(
    client: FlatfoxPublicClient,
    *,
    limit: int,
    offset: int,
    max_pages: int,
    status: str,
    selection: int | None,
    pk: list[int] | None,
    sleep_seconds: float,
) -> tuple[dict[str, Any], ...]:
    pages: list[dict[str, Any]] = []
    next_url = build_public_listing_url(
        client.base_url,
        limit=limit,
        offset=offset,
        status=status,
        selection=selection,
        pk=pk,
    )
    while next_url and len(pages) < max_pages:
        page = client.get_json(next_url)
        pages.append(page)
        next_url = page.get("next") or ""
        if next_url and sleep_seconds > 0 and len(pages) < max_pages:
            time.sleep(sleep_seconds)
    return tuple(pages)


def resolve_area_preset(area: str) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if area == "none":
        return None
    if area not in AREA_PRESETS:
        raise SystemExit(f"Unknown --area '{area}'. Choose one of: {', '.join(sorted([*AREA_PRESETS, 'none']))}.")
    return AREA_PRESETS[area]


def parse_bbox(values: list[float] | None) -> tuple[float, float, float, float] | None:
    if values is None:
        return None
    if len(values) != 4:
        raise SystemExit("--bbox requires exactly four values: SOUTH WEST NORTH EAST.")
    south, west, north, east = values
    if south > north or west > east:
        raise SystemExit("--bbox values must be SOUTH WEST NORTH EAST.")
    return south, west, north, east


def sync_flatfox_public(args: argparse.Namespace) -> SyncStats:
    cities = target_city_names(args.city)
    corridor = resolve_area_preset(args.area)
    bbox = parse_bbox(args.bbox)
    client = FlatfoxPublicClient(
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )
    stats = SyncStats()

    with closing(connect(args.db)) as conn:
        init_db(conn)
        pages = iter_public_listing_pages(
            client,
            limit=args.limit,
            offset=args.offset,
            max_pages=args.max_pages,
            status=args.status,
            selection=args.selection,
            pk=args.pk,
            sleep_seconds=args.sleep_seconds,
        )
        for page in pages:
            stats = stats.add(pages=1)
            for item in page.get("results") or []:
                stats = stats.add(fetched=1)
                should_ingest, reason = should_ingest_public_listing(
                    item,
                    cities=cities,
                    corridor=corridor,
                    corridor_km=args.corridor_km,
                    bbox=bbox,
                    max_rent=args.max_rent,
                    include_over_budget=args.include_over_budget,
                    include_unknown_rent=args.include_unknown_rent,
                )
                if not should_ingest:
                    stats = stats.add(skipped=1)
                    if args.verbose:
                        print(f"skip {item.get('pk')}: {reason}")
                    continue

                listing = listing_input_from_public_listing(item, args.base_url)
                scored = score_listing(normalize_listing(listing))
                listing_id, created = upsert_listing(conn, scored)
                stats = stats.add(matched=1, created=1 if created else 0, updated=0 if created else 1)
                if args.verbose:
                    print(
                        f"{'created' if created else 'updated'} #{listing_id}: "
                        f"{scored.decision} score={scored.priority_score} "
                        f"CHF {listing.rent_chf or 'unknown'} {listing.city} {listing.title}"
                    )

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch documented Flatfox public listings and ingest relevant ones into the local tracker."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite tracker path.")
    parser.add_argument("--base-url", default=FLATFOX_BASE_URL)
    parser.add_argument("--limit", type=int, default=100, help="Flatfox API page size.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=3, help="Safety cap for pages fetched in one run.")
    parser.add_argument("--sleep-seconds", type=float, default=1.0, help="Pause between paginated requests.")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-retries", type=int, default=2, help="Retries for transient Flatfox errors, excluding 429.")
    parser.add_argument("--status", default="act")
    parser.add_argument(
        "--selection",
        type=int,
        help="Optional documented Flatfox selection ID for server-side filtering.",
    )
    parser.add_argument("--pk", type=int, action="append", help="Fetch one or more exact Flatfox listing IDs.")
    parser.add_argument("--city", action="append", default=DEFAULT_TARGET_CITIES, help="Target city. Repeatable.")
    parser.add_argument(
        "--area",
        default=DEFAULT_AREA,
        choices=[*sorted(AREA_PRESETS), "none"],
        help="Local coordinate filter preset. Use 'none' for exact city matching only.",
    )
    parser.add_argument(
        "--corridor-km",
        type=float,
        default=DEFAULT_CORRIDOR_KM,
        help="Half-width in km for the local area corridor.",
    )
    parser.add_argument(
        "--bbox",
        type=float,
        nargs=4,
        metavar=("SOUTH", "WEST", "NORTH", "EAST"),
        help="Optional local bounding box filter using Flatfox latitude/longitude.",
    )
    parser.add_argument("--max-rent", type=int, default=MAX_RENT_CHF)
    parser.add_argument("--include-over-budget", action="store_true")
    parser.add_argument("--include-unknown-rent", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1.")
    if args.limit < 1 or args.limit > 100:
        raise SystemExit("--limit must be between 1 and 100.")
    stats = sync_flatfox_public(args)
    print(
        "Flatfox public sync complete: "
        f"pages={stats.pages}, fetched={stats.fetched}, matched={stats.matched}, "
        f"created={stats.created}, updated={stats.updated}, skipped={stats.skipped}"
    )
    print("No applications were sent. Review queue/drafts and approve listings manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
