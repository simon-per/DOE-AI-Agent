import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from execution import commute_scoring as cs
from execution import transit_scoring as ts


def _fake_connections(*durations: str) -> dict:
    return {"connections": [{"duration": d} for d in durations]}


class DurationParseTest(unittest.TestCase):
    def test_parses_standard_duration(self) -> None:
        self.assertEqual(ts.parse_duration_to_minutes("00d00:43:00"), 43)
        self.assertEqual(ts.parse_duration_to_minutes("00d01:05:00"), 65)
        self.assertEqual(ts.parse_duration_to_minutes("01d00:00:00"), 1440)

    def test_rounds_seconds_and_floors_at_one(self) -> None:
        self.assertEqual(ts.parse_duration_to_minutes("00d00:12:31"), 13)  # rounds up
        self.assertEqual(ts.parse_duration_to_minutes("00d00:12:29"), 12)  # rounds down
        self.assertEqual(ts.parse_duration_to_minutes("00d00:00:05"), 1)   # floor at 1

    def test_rejects_garbage(self) -> None:
        self.assertIsNone(ts.parse_duration_to_minutes(""))
        self.assertIsNone(ts.parse_duration_to_minutes("43 minutes"))
        self.assertIsNone(ts.parse_duration_to_minutes(None))  # type: ignore[arg-type]


class QueryTransitTest(unittest.TestCase):
    def test_returns_minimum_duration_across_connections(self) -> None:
        with patch.object(
            cs, "_http_json",
            return_value=_fake_connections("00d00:43:00", "00d00:31:00", "00d00:52:00"),
        ):
            self.assertEqual(ts._query_transit_minutes("Luzern", "Root D4"), 31)

    def test_none_when_no_connections(self) -> None:
        with patch.object(cs, "_http_json", return_value={"connections": []}):
            self.assertIsNone(ts._query_transit_minutes("Nowhere", "Root D4"))

    def test_none_when_http_fails(self) -> None:
        with patch.object(cs, "_http_json", return_value=None):
            self.assertIsNone(ts._query_transit_minutes("Luzern", "Root D4"))

    def test_skips_unparseable_durations(self) -> None:
        with patch.object(
            cs, "_http_json", return_value=_fake_connections("garbage", "00d00:40:00")
        ):
            self.assertEqual(ts._query_transit_minutes("Luzern", "Root D4"), 40)


class LiveTransitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=cs.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "commute.sqlite"
        # conftest disables transit suite-wide; these tests drive the real
        # live_transit_minutes (with HTTP mocked), so opt back in.
        enabled = patch.object(ts, "is_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_query_then_cache(self) -> None:
        with patch.object(cs, "_http_json", return_value=_fake_connections("00d00:18:00")):
            first = ts.live_transit_minutes("Luzern", cache_path=self.cache_path)
        self.assertIsNotNone(first)
        self.assertEqual(first.minutes, 18)
        self.assertEqual(first.mode, ts.TRANSIT_MODE)
        # Second call is a cache hit — no further HTTP.
        with patch.object(cs, "_http_json") as http2:
            second = ts.live_transit_minutes("Luzern", cache_path=self.cache_path)
        self.assertEqual(second.minutes, 18)
        http2.assert_not_called()

    def test_empty_address_returns_none(self) -> None:
        with patch.object(cs, "_http_json") as http:
            self.assertIsNone(ts.live_transit_minutes("   ", cache_path=self.cache_path))
        http.assert_not_called()

    def test_transit_and_ebike_coexist_in_shared_cache(self) -> None:
        # The transit work-key suffix must keep the oeV row from clobbering the
        # e-bike row for the same origin in the shared commute_cache.
        work_key = cs.normalize_key(cs.WORK_DEFAULT_ADDRESS)
        with closing(cs.init_cache(self.cache_path)) as conn:
            cs.cache_put(
                conn,
                cs.normalize_key("Luzern"),
                work_key,
                cs.RouteResult(minutes=35, mode="e-bike (live)", distance_km=9.1),
            )
        with patch.object(cs, "_http_json", return_value=_fake_connections("00d00:16:00")):
            transit = ts.live_transit_minutes("Luzern", cache_path=self.cache_path)
        self.assertEqual(transit.minutes, 16)
        # The e-bike row must still be intact under its own (unsuffixed) key.
        with closing(cs.init_cache(self.cache_path)) as conn:
            ebike = cs.cache_get(conn, cs.normalize_key("Luzern"), work_key)
        self.assertEqual(ebike.minutes, 35)
        self.assertEqual(ebike.mode, "e-bike (live)")


class PipelineTransitIntegrationTest(unittest.TestCase):
    def test_transit_only_scores_when_ors_disabled(self) -> None:
        # The Luzern case: no ORS key configured, but keyless transit lifts the
        # listing from B (≈35 min e-bike) to A (≤30 min oeV).
        from execution.apartment_pipeline import estimate_commute

        with patch.object(cs, "api_key", return_value=None), \
             patch("execution.transit_scoring.live_transit_minutes",
                   return_value=cs.RouteResult(minutes=25, mode="oeV (live)", distance_km=None)):
            minutes, klass, mode = estimate_commute("Luzern")
        self.assertEqual(minutes, 25)
        self.assertEqual(klass, "A")
        self.assertEqual(mode, "oeV (live)")

    def test_min_of_ebike_and_transit_wins_with_combined_label(self) -> None:
        from execution.apartment_pipeline import estimate_commute

        with patch("execution.commute_scoring.live_commute_minutes",
                   return_value=cs.RouteResult(minutes=35, mode="e-bike (live)", distance_km=9.0)), \
             patch.object(cs, "is_enabled", return_value=True), \
             patch("execution.transit_scoring.live_transit_minutes",
                   return_value=cs.RouteResult(minutes=22, mode="oeV (live)", distance_km=None)):
            minutes, klass, mode = estimate_commute("Luzern")
        self.assertEqual(minutes, 22)       # transit is the faster leg
        self.assertEqual(klass, "A")
        self.assertIn("oeV 22", mode)
        self.assertIn("e-bike 35", mode)


if __name__ == "__main__":
    unittest.main()
