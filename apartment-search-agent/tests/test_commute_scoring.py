import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from execution import commute_scoring as cs


def _fake_geocode_response(lat: float, lng: float) -> dict:
    return {"features": [{"geometry": {"coordinates": [lng, lat]}}]}


def _fake_route_response(seconds: float, meters: float) -> dict:
    return {"routes": [{"summary": {"duration": seconds, "distance": meters}}]}


class HelperTest(unittest.TestCase):
    def test_normalize_key_lowercases_and_collapses_whitespace(self) -> None:
        self.assertEqual(cs.normalize_key("  Buchrain   LU "), "buchrain lu")
        self.assertEqual(cs.normalize_key(""), "")

    def test_cache_fresh_window(self) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        recent = (now - timedelta(days=1)).isoformat()
        stale = (now - timedelta(days=cs.CACHE_TTL_DAYS + 1)).isoformat()
        self.assertTrue(cs._cache_fresh(recent, cs.CACHE_TTL_DAYS))
        self.assertFalse(cs._cache_fresh(stale, cs.CACHE_TTL_DAYS))
        self.assertFalse(cs._cache_fresh("not-a-date", cs.CACHE_TTL_DAYS))


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=cs.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "commute.sqlite"

    def test_init_cache_creates_both_tables(self) -> None:
        with closing(cs.init_cache(self.cache_path)) as conn:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("commute_cache", names)
        self.assertIn("geocode_cache", names)

    def test_cache_put_and_get_roundtrip(self) -> None:
        with closing(cs.init_cache(self.cache_path)) as conn:
            cs.cache_put(
                conn, "buchrain", "platz 4",
                cs.RouteResult(minutes=22, mode="e-bike (live)", distance_km=6.5),
            )
            got = cs.cache_get(conn, "buchrain", "platz 4")
        self.assertEqual(got.minutes, 22)
        self.assertEqual(got.mode, "e-bike (live)")
        self.assertEqual(got.distance_km, 6.5)

    def test_cache_get_misses_when_keys_differ(self) -> None:
        with closing(cs.init_cache(self.cache_path)) as conn:
            cs.cache_put(
                conn, "buchrain", "platz 4",
                cs.RouteResult(minutes=22, mode="e-bike (live)", distance_km=6.5),
            )
            self.assertIsNone(cs.cache_get(conn, "ebikon", "platz 4"))


class LiveLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=cs.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "commute.sqlite"

    def test_returns_none_without_api_key(self) -> None:
        with patch.object(cs, "api_key", return_value=None):
            result = cs.live_commute_minutes("Buchrain", cache_path=self.cache_path)
        self.assertIsNone(result)

    def test_cache_hit_skips_http(self) -> None:
        with closing(cs.init_cache(self.cache_path)) as conn:
            cs.cache_put(
                conn,
                cs.normalize_key("Buchrain, Switzerland"),
                cs.normalize_key(cs.WORK_DEFAULT_ADDRESS),
                cs.RouteResult(minutes=21, mode="e-bike (live)", distance_km=6.4),
            )
        with patch.object(cs, "api_key", return_value="fake-key"), \
             patch.object(cs, "_http_json") as http:
            result = cs.live_commute_minutes(
                "Buchrain, Switzerland", cache_path=self.cache_path,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.minutes, 21)
        http.assert_not_called()

    def test_full_path_geocodes_then_routes_then_caches(self) -> None:
        # Sequence: geocode origin → route_ebike → cache_put
        calls = []

        def fake_http(url, **kwargs):
            calls.append((url, kwargs.get("data") is not None))
            if "geocode" in url:
                return _fake_geocode_response(lat=47.13, lng=8.45)
            return _fake_route_response(seconds=20 * 60, meters=6800)

        with patch.object(cs, "api_key", return_value="fake-key"), \
             patch.object(cs, "_http_json", side_effect=fake_http):
            result = cs.live_commute_minutes(
                "Ebikon, Switzerland", cache_path=self.cache_path,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result.minutes, 20)
        self.assertEqual(result.mode, "e-bike (live)")
        self.assertEqual(result.distance_km, 6.8)

        # Re-call should be a cache hit (no further http)
        with patch.object(cs, "api_key", return_value="fake-key"), \
             patch.object(cs, "_http_json") as http2:
            result2 = cs.live_commute_minutes(
                "Ebikon, Switzerland", cache_path=self.cache_path,
            )
        self.assertEqual(result2.minutes, 20)
        http2.assert_not_called()

    def test_geocoder_failure_returns_none(self) -> None:
        with patch.object(cs, "api_key", return_value="fake-key"), \
             patch.object(cs, "_http_json", return_value=None):
            result = cs.live_commute_minutes(
                "Nowhere, Switzerland", cache_path=self.cache_path,
            )
        self.assertIsNone(result)


class PipelineIntegrationTest(unittest.TestCase):
    """estimate_commute() must keep working when the ORS path is disabled."""

    def test_estimate_commute_uses_static_when_api_disabled(self) -> None:
        from execution.apartment_pipeline import estimate_commute

        with patch.object(cs, "api_key", return_value=None):
            minutes, klass, mode = estimate_commute("Buchrain")
        self.assertEqual(minutes, 22)
        self.assertEqual(klass, "A")
        self.assertIn("e-bike", mode)

    def test_estimate_commute_prefers_live_when_available(self) -> None:
        from execution.apartment_pipeline import estimate_commute

        with patch(
            "execution.commute_scoring.live_commute_minutes",
            return_value=cs.RouteResult(minutes=15, mode="e-bike (live)", distance_km=4.2),
        ), patch.object(cs, "is_enabled", return_value=True):
            minutes, klass, mode = estimate_commute("Some custom address")
        self.assertEqual(minutes, 15)
        self.assertEqual(klass, "A+")
        self.assertEqual(mode, "e-bike (live)")

    def test_live_lookup_failure_falls_back_to_static(self) -> None:
        from execution.apartment_pipeline import estimate_commute

        with patch(
            "execution.commute_scoring.live_commute_minutes",
            side_effect=RuntimeError("ORS down"),
        ), patch.object(cs, "is_enabled", return_value=True):
            minutes, klass, mode = estimate_commute("Ebikon")
        # Falls back to static table entry for Ebikon (27 min, A).
        self.assertEqual(minutes, 27)
        self.assertEqual(klass, "A")


if __name__ == "__main__":
    unittest.main()
