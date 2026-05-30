import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from execution.apartment_pipeline import (
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    score_listing,
    upsert_listing,
)
from execution.flatfox_public_sync import (
    FLATFOX_BASE_URL,
    FlatfoxApiError,
    flatfox_pk_from_url,
    reconcile_active_listings,
)

OLD_SEEN = "2026-05-20T00:00:00+00:00"  # well before any 20h cutoff in 2026-05


class FakeReconcileClient:
    """Returns the requested pks intersected with `active_pks` (plus optional
    `extra_pks` to simulate a server that ignored the pk filter)."""

    def __init__(self, active_pks, *, raise_exc=None, extra_pks=()):
        self.base_url = FLATFOX_BASE_URL
        self.active_pks = set(active_pks)
        self.raise_exc = raise_exc
        self.extra_pks = set(extra_pks)
        self.calls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.calls.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        requested = {int(p) for p in parse_qs(urlsplit(url).query).get("pk", [])}
        returned = (requested & self.active_pks) | self.extra_pks
        return {"results": [{"pk": pk, "status": "act"} for pk in sorted(returned)]}


def _seed_flatfox(conn, pk, *, decision="consider", last_seen=OLD_SEEN, city="Luzern"):
    listing = ListingInput(
        url=f"https://flatfox.ch/en/flat/test-{pk}/{pk}/",
        source="flatfox.ch",
        title=f"WG {city} {pk}",
        rent_chf=850,
        city=city,
        move_in="16.07.",
        contact_name=None,
        contact_email=None,
        raw_text=f"ruhig sauber Anmeldung Zimmer {pk}",
        commute_minutes=None,
    )
    listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(listing)))
    conn.execute(
        "UPDATE listings SET decision = ?, last_seen = ? WHERE id = ?",
        (decision, last_seen, listing_id),
    )
    conn.commit()
    return listing_id


def _status(conn, listing_id):
    return conn.execute(
        "SELECT status, expired_at, last_seen FROM listings WHERE id = ?", (listing_id,)
    ).fetchone()


class PkParseTest(unittest.TestCase):
    def test_parses_trailing_pk(self) -> None:
        self.assertEqual(
            flatfox_pk_from_url("https://flatfox.ch/en/flat/kirchbreiteweg-1-6033-buchrain/86023452/"),
            86023452,
        )

    def test_parses_without_trailing_slash(self) -> None:
        self.assertEqual(flatfox_pk_from_url("https://flatfox.ch/en/flat/foo/123"), 123)

    def test_none_when_unparseable(self) -> None:
        self.assertIsNone(flatfox_pk_from_url("https://flatfox.ch/en/flat/no-pk/"))
        self.assertIsNone(flatfox_pk_from_url(""))
        self.assertIsNone(flatfox_pk_from_url(None))


class ReconcileTest(unittest.TestCase):
    def test_expires_gone_keeps_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            live = _seed_flatfox(conn, 111)
            gone = _seed_flatfox(conn, 222)
            client = FakeReconcileClient(active_pks={111})
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual((stats.still_active, stats.expired), (1, 1))
            live_row, gone_row = _status(conn, live), _status(conn, gone)
            self.assertEqual(live_row["status"], "new")
            self.assertIsNone(live_row["expired_at"])
            self.assertNotEqual(live_row["last_seen"], OLD_SEEN)  # refreshed
            self.assertEqual(gone_row["status"], "expired")
            self.assertIsNotNone(gone_row["expired_at"])

    def test_never_expires_on_api_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            a = _seed_flatfox(conn, 111)
            b = _seed_flatfox(conn, 222)
            client = FakeReconcileClient(active_pks=set(), raise_exc=FlatfoxApiError("boom"))
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual(stats.expired, 0)
            self.assertEqual(stats.skipped, 2)
            self.assertEqual(_status(conn, a)["status"], "new")
            self.assertEqual(_status(conn, b)["status"], "new")

    def test_ambiguous_response_skips_batch(self) -> None:
        # Server returns a pk we did not request -> pk filter ignored -> inconclusive.
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            a = _seed_flatfox(conn, 111)
            b = _seed_flatfox(conn, 222)
            client = FakeReconcileClient(active_pks={111}, extra_pks={999})
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual(stats.expired, 0)
            self.assertEqual(stats.skipped, 2)
            self.assertEqual(_status(conn, a)["status"], "new")
            self.assertEqual(_status(conn, b)["status"], "new")

    def test_unparseable_url_skipped_without_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            listing_id = _seed_flatfox(conn, 111)
            conn.execute(
                "UPDATE listings SET url = ?, canonical_url = ? WHERE id = ?",
                ("https://flatfox.ch/en/flat/no-pk/", "https://flatfox.ch/en/flat/no-pk/", listing_id),
            )
            conn.commit()
            client = FakeReconcileClient(active_pks=set())
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual(client.calls, [])  # nothing parseable -> no API call
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(stats.expired, 0)
            self.assertEqual(_status(conn, listing_id)["status"], "new")

    def test_dry_run_makes_zero_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            _seed_flatfox(conn, 111)
            _seed_flatfox(conn, 222)
            client = FakeReconcileClient(active_pks=set())
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10, dry_run=True)

            self.assertEqual(client.calls, [])
            self.assertEqual(stats.candidates, 2)
            self.assertEqual(stats.expired, 0)

    def test_fresh_listings_not_candidates(self) -> None:
        # A listing re-seen within the window (fresh last_seen) is not checked.
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            fresh = _seed_flatfox(conn, 111)
            conn.execute("UPDATE listings SET last_seen = updated_at WHERE id = ?", (fresh,))
            conn.commit()
            _seed_flatfox(conn, 222)  # OLD_SEEN -> stale
            client = FakeReconcileClient(active_pks={222})
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual(stats.candidates, 1)  # only the stale one
            self.assertEqual(_status(conn, fresh)["status"], "new")

    def test_max_checks_caps_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            _seed_flatfox(conn, 111)
            _seed_flatfox(conn, 222)
            _seed_flatfox(conn, 333)
            client = FakeReconcileClient(active_pks={111, 222, 333})
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=2)

            self.assertEqual(stats.candidates, 2)  # LIMIT 2

    def test_skip_decision_not_checked(self) -> None:
        # Only actionable tiers are reconciled; a 'skip' listing is left alone.
        with tempfile.TemporaryDirectory() as tmpdir, \
             closing(connect(Path(tmpdir) / "listings.sqlite")) as conn:
            init_db(conn)
            skipped = _seed_flatfox(conn, 111, decision="skip")
            client = FakeReconcileClient(active_pks=set())
            stats = reconcile_active_listings(conn, client, stale_hours=20, max_checks=10)

            self.assertEqual(stats.candidates, 0)
            self.assertEqual(client.calls, [])
            self.assertEqual(_status(conn, skipped)["status"], "new")


if __name__ == "__main__":
    unittest.main()
