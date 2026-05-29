import argparse
import tempfile
import unittest
import urllib.error
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from execution.apartment_pipeline import (
    ListingInput,
    approve_listing,
    connect,
    mark_sent,
    normalize_listing,
    safe_daily_plan,
    score_listing,
    upsert_listing,
)
from execution.flatfox_public_sync import (
    FLATFOX_BASE_URL,
    TRACKER_SOURCE,
    FlatfoxRateLimitError,
    FlatfoxPublicClient,
    attribute_name,
    build_public_listing_url,
    point_to_segment_distance_km,
    listing_input_from_public_listing,
    should_ingest_public_listing,
    sync_flatfox_public,
)


ROOT_SHARED_LISTING = {
    "pk": 123,
    "url": "/en/flat/6037-root/123/",
    "short_url": "/123/",
    "status": "act",
    "offer_type": "RENT",
    "object_category": "SHARED",
    "object_type": "SHARED_FLAT",
    "price_display": 760,
    "price_unit": "monthly",
    "rent_gross": 760,
    "short_title": "Room in shared flat",
    "public_title": "WG Zimmer in Root - CHF 760 incl. utilities per month",
    "description": "ruhig sauber Anmeldung Veloplatz",
    "city": "Root",
    "zipcode": 6037,
    "public_address": "6037 Root",
    "moving_date": "2026-07-16",
    "is_furnished": True,
    "is_temporary": False,
    "is_selling_furniture": False,
    "attributes": [],
}


class FakeFlatfoxClient:
    def __init__(self, *args, **kwargs) -> None:
        self.base_url = FLATFOX_BASE_URL

    def get_json(self, url: str) -> dict:
        return {
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                ROOT_SHARED_LISTING,
                {
                    **ROOT_SHARED_LISTING,
                    "pk": 124,
                    "url": "/en/flat/parking/124/",
                    "object_category": "PARK",
                    "object_type": "GARAGE_SLOT",
                    "public_title": "Parking in Root",
                    "price_display": 130,
                    "rent_gross": None,
                },
            ],
        }


def sync_args(db_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=db_path,
        base_url=FLATFOX_BASE_URL,
        timeout_seconds=30,
        max_retries=2,
        limit=100,
        offset=0,
        max_pages=1,
        status="act",
        selection=None,
        pk=None,
        sleep_seconds=0,
        city=["Root"],
        area="luzern-rotkreuz",
        corridor_km=5.0,
        bbox=None,
        max_rent=1000,
        include_over_budget=False,
        include_unknown_rent=False,
        verbose=False,
    )


class FlatfoxPublicSyncTest(unittest.TestCase):
    def test_build_public_listing_url_uses_documented_params(self) -> None:
        url = build_public_listing_url(
            FLATFOX_BASE_URL,
            limit=100,
            offset=200,
            status="act",
            selection=42,
            pk=[123, 456],
            expand=["cover_image"],
            include=["agency"],
        )

        self.assertIn("/api/v1/public-listing/", url)
        self.assertIn("limit=100", url)
        self.assertIn("offset=200", url)
        self.assertIn("status=act", url)
        self.assertIn("selection=42", url)
        self.assertIn("pk=123", url)
        self.assertIn("pk=456", url)
        self.assertIn("expand=cover_image", url)
        self.assertIn("include=agency", url)

    def test_should_ingest_target_listing_only(self) -> None:
        cities = {"root"}
        self.assertEqual(
            should_ingest_public_listing(
                ROOT_SHARED_LISTING,
                cities=cities,
                corridor=None,
                corridor_km=10.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=False,
            ),
            (True, "matched"),
        )

        over_budget = {**ROOT_SHARED_LISTING, "rent_gross": 1200, "price_display": 1200}
        self.assertEqual(
            should_ingest_public_listing(
                over_budget,
                cities=cities,
                corridor=None,
                corridor_km=10.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=False,
            ),
            (False, "over budget"),
        )

        parking = {**ROOT_SHARED_LISTING, "object_category": "PARK", "object_type": "GARAGE_SLOT"}
        self.assertEqual(
            should_ingest_public_listing(
                parking,
                cities=cities,
                corridor=None,
                corridor_km=10.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=False,
            ),
            (False, "not target object type"),
        )

        weekly = {**ROOT_SHARED_LISTING, "price_unit": "weekly", "rent_gross": 300, "price_display": 300}
        self.assertEqual(
            should_ingest_public_listing(
                weekly,
                cities=cities,
                corridor=None,
                corridor_km=10.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=True,
            ),
            (False, "non-monthly price unit"),
        )

    def test_corridor_area_matches_between_luzern_and_rotkreuz(self) -> None:
        corridor = ((47.0502, 8.3093), (47.1416, 8.4314))
        root_without_city_match = {
            **ROOT_SHARED_LISTING,
            "city": "Dietwil",
            "latitude": 47.104,
            "longitude": 8.373,
        }
        far_listing = {
            **ROOT_SHARED_LISTING,
            "city": "Zuerich",
            "latitude": 47.3769,
            "longitude": 8.5417,
        }

        self.assertLess(point_to_segment_distance_km((47.104, 8.373), corridor[0], corridor[1]), 5.0)
        self.assertEqual(
            should_ingest_public_listing(
                root_without_city_match,
                cities={"root"},
                corridor=corridor,
                corridor_km=5.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=False,
            ),
            (True, "matched"),
        )
        self.assertEqual(
            should_ingest_public_listing(
                far_listing,
                cities={"root"},
                corridor=corridor,
                corridor_km=5.0,
                bbox=None,
                max_rent=1000,
                include_over_budget=False,
                include_unknown_rent=False,
            ),
            (False, "outside target area"),
        )

    def test_public_listing_maps_to_tracker_input(self) -> None:
        listing = listing_input_from_public_listing(
            {**ROOT_SHARED_LISTING, "attributes": [{"name": "Balkon"}, {"label": "Lift"}]},
            FLATFOX_BASE_URL,
        )

        self.assertEqual(listing.source, TRACKER_SOURCE)
        self.assertEqual(listing.rent_chf, 760)
        self.assertEqual(listing.city, "Root")
        self.assertEqual(listing.move_in, "2026-07-16")
        self.assertIn("https://flatfox.ch/en/flat/6037-root/123/", listing.url or "")
        self.assertIn("moebliert", listing.raw_text)
        self.assertIn("Balkon, Lift", listing.raw_text)
        self.assertEqual(attribute_name({"name": "Washer"}), "Washer")

    def test_sync_ingests_only_relevant_flatfox_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            args = sync_args(db_path)

            with patch("execution.flatfox_public_sync.FlatfoxPublicClient", FakeFlatfoxClient):
                stats = sync_flatfox_public(args)
                stats_second = sync_flatfox_public(args)

            self.assertEqual(stats.fetched, 2)
            self.assertEqual(stats.matched, 1)
            self.assertEqual(stats.created, 1)
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(stats_second.created, 0)
            self.assertEqual(stats_second.updated, 1)

    def test_flatfox_source_counts_against_same_site_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                manual_listing = ListingInput(
                    url="https://flatfox.ch/en/flat/manual/999/",
                    source="flatfox.ch",
                    title="Manual Flatfox Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                manual_id, _ = upsert_listing(conn, score_listing(normalize_listing(manual_listing)))
                approve_listing(conn, manual_id, note="approved")
                mark_sent(conn, manual_id, note="sent")

            args = sync_args(db_path)
            with patch("execution.flatfox_public_sync.FlatfoxPublicClient", FakeFlatfoxClient):
                sync_flatfox_public(args)

            with closing(connect(db_path)) as conn:
                plan = safe_daily_plan(conn, daily_limit=5, site_daily_limit=1)

            self.assertEqual(plan, [])

    def test_rate_limit_raises_clear_error(self) -> None:
        headers = {"Retry-After": "60"}
        error = urllib.error.HTTPError(
            url="https://flatfox.ch/api/v1/public-listing/",
            code=429,
            msg="Too Many Requests",
            hdrs=headers,
            fp=BytesIO(b"slow down"),
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(FlatfoxRateLimitError) as ctx:
                FlatfoxPublicClient(max_retries=0).get_json("https://flatfox.ch/api/v1/public-listing/")

        self.assertIn("Retry after 60 seconds", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
