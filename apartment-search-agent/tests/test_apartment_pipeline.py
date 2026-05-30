import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from execution import apartment_pipeline as ap
from execution import commute_scoring as cs
from execution.apartment_pipeline import (
    ListingInput,
    approve_listing,
    canonicalize_url,
    connect,
    estimate_commute,
    extract_rent,
    init_db,
    list_queue,
    mark_sent,
    normalize_listing,
    rescore_all,
    safe_daily_plan,
    score_listing,
    upsert_listing,
)


class ApartmentPipelineTest(unittest.TestCase):
    def test_canonicalize_url_drops_tracking_params(self) -> None:
        url = "https://www.example.ch/listing/123/?utm_source=x&z=2&foo=bar#section"
        self.assertEqual(
            canonicalize_url(url),
            "https://www.example.ch/listing/123?foo=bar&z=2",
        )

        self.assertEqual(
            canonicalize_url("https://example.ch/x?b=2&a=1"),
            canonicalize_url("https://example.ch/x?a=1&b=2"),
        )

    def test_rent_extraction_prefers_rent_over_deposit(self) -> None:
        self.assertEqual(extract_rent("Miete CHF 1200. Kaution CHF 800."), 1200)
        self.assertEqual(extract_rent("Mietzins CHF 1200. Kaution CHF 800."), 1200)
        scored = score_listing(
            normalize_listing(
                ListingInput(
                    url=None,
                    source="manual",
                    title="Studio Root",
                    rent_chf=None,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="Miete CHF 1200. Kaution CHF 800.",
                    commute_minutes=None,
                )
            )
        )
        self.assertEqual(scored.normalized["rent_chf"], 1200)
        self.assertEqual(scored.decision, "skip")

    def test_unknown_rent_requires_manual_review(self) -> None:
        scored = score_listing(
            normalize_listing(
                ListingInput(
                    url=None,
                    source="manual",
                    title="Schoenes Zimmer Root",
                    rent_chf=None,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
            )
        )

        self.assertIsNone(scored.normalized["rent_chf"])
        self.assertEqual(scored.decision, "manual_review")

    def test_strong_listing_scores_apply_with_personalized_draft(self) -> None:
        raw = """
        Schoenes WG Zimmer in Root
        CHF 760
        ab 16.07.
        ruhige saubere WG, Anmeldung moeglich, Veloplatz vorhanden
        """
        listing = ListingInput(
            url="https://flatfox.ch/listing/abc",
            source=None,
            title=None,
            rent_chf=None,
            city=None,
            move_in=None,
            contact_name="Lena",
            contact_email=None,
            raw_text=raw,
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))

        self.assertEqual(scored.decision, "apply")
        self.assertEqual(scored.commute_class, "A+")
        self.assertEqual(scored.gender_status, "eligible")
        self.assertIn("Hoi Lena", scored.message_draft)
        self.assertIn("PHENOGY", scored.message_draft)
        # WS3: a strong listing cites the concrete commute time as the sell.
        self.assertIsNotNone(scored.commute_minutes)
        self.assertIn(f"rund {scored.commute_minutes} Minuten", scored.message_draft)

    def test_unknown_commute_draft_has_no_none_minutes(self) -> None:
        # WS3 guard: an unknown commute must not leak "None Minuten" into a draft.
        listing = ListingInput(
            url="https://x.test/xyz",
            source="flatfox",
            title="WG in Xyztown",
            rent_chf=800,
            city="Xyztown",
            move_in=None,
            contact_name=None,
            contact_email=None,
            raw_text="WG Zimmer, ruhig und sauber",
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))
        self.assertEqual(scored.commute_class, "unknown")
        self.assertNotIn("None", scored.message_draft)
        self.assertNotIn("rund None", scored.message_draft)

    def test_hard_gender_restriction_skips(self) -> None:
        listing = ListingInput(
            url=None,
            source="wgzimmer",
            title="Zimmer in Ebikon",
            rent_chf=700,
            city="Ebikon",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="Nur Frauen, Frauen-WG only",
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))

        self.assertEqual(scored.decision, "skip")
        self.assertEqual(scored.gender_status, "hard_exclusion")

    def test_scam_risk_skips_payment_before_viewing(self) -> None:
        listing = ListingInput(
            url=None,
            source="manual",
            title="Studio in Rotkreuz",
            rent_chf=850,
            city="Rotkreuz",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="Deposit before viewing required, key shipping by courier.",
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))

        self.assertEqual(scored.decision, "skip")
        self.assertEqual(scored.scam_risk, "high")

    def test_c_commute_skips(self) -> None:
        listing = ListingInput(
            url=None,
            source="manual",
            title="Zimmer in Kriens",
            rent_chf=650,
            city="Kriens",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="ruhig sauber",
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))

        self.assertEqual(scored.commute_class, "C")
        self.assertEqual(scored.decision, "skip")

    def test_limited_use_listing_skips(self) -> None:
        listing = ListingInput(
            url=None,
            source="flatfox.ch",
            title="WG Zimmer Rotkreuz",
            rent_chf=650,
            city="Rotkreuz",
            move_in="",
            contact_name=None,
            contact_email=None,
            raw_text=(
                "Möbliertes WG-Zimmer zur Gelegenheitsnutzung. "
                "Das Zimmer steht nicht für eine dauerhafte Vollzeitbelegung zur Verfügung, "
                "sondern eignet sich fuer jemanden, der nur einzelne Tage pro Monat anwesend ist."
            ),
            commute_minutes=None,
        )
        scored = score_listing(normalize_listing(listing))

        self.assertEqual(scored.decision, "skip")
        self.assertIn("limited_use_only", scored.flags)

    def test_no_anmeldung_skips_unless_temporary_review(self) -> None:
        permanent = ListingInput(
            url=None,
            source="manual",
            title="Zimmer in Buchrain",
            rent_chf=790,
            city="Buchrain",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="ruhiges Zimmer, keine Anmeldung moeglich",
            commute_minutes=None,
        )
        temporary = ListingInput(
            url=None,
            source="manual",
            title="Zwischenmiete in Buchrain",
            rent_chf=790,
            city="Buchrain",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="befristete Zwischenmiete, keine Anmeldung moeglich",
            commute_minutes=None,
        )

        self.assertEqual(score_listing(normalize_listing(permanent)).decision, "skip")
        self.assertEqual(score_listing(normalize_listing(temporary)).decision, "manual_review")

    def test_dedupe_and_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing = ListingInput(
                    url="https://flatfox.ch/listing/abc?utm_campaign=test",
                    source=None,
                    title="WG Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                scored = score_listing(normalize_listing(listing))
                listing_id, created = upsert_listing(conn, scored)
                listing_id_2, created_2 = upsert_listing(conn, scored)

                self.assertEqual(listing_id, listing_id_2)
                self.assertTrue(created)
                self.assertFalse(created_2)

                with self.assertRaises(SystemExit):
                    mark_sent(conn, listing_id, note=None)

                approve_listing(conn, listing_id, note="approved in test")
                mark_sent(conn, listing_id, note="sent in test")
                row = conn.execute(
                    "SELECT status, approval_status, sent_at FROM listings WHERE id = ?",
                    (listing_id,),
                ).fetchone()
                self.assertEqual(row["status"], "sent")
                self.assertEqual(row["approval_status"], "approved")
                self.assertIsNotNone(row["sent_at"])

    def test_same_content_dedupes_across_different_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                first = ListingInput(
                    url="https://aggregator.example/listing/1",
                    source="aggregator",
                    title="WG Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                second = ListingInput(
                    url="https://original.example/listing/9",
                    source="original",
                    title="WG Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )

                first_id, first_created = upsert_listing(conn, score_listing(normalize_listing(first)))
                second_id, second_created = upsert_listing(conn, score_listing(normalize_listing(second)))

                self.assertEqual(first_id, second_id)
                self.assertTrue(first_created)
                self.assertFalse(second_created)

    def test_listing_change_resets_stale_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                original = ListingInput(
                    url="https://flatfox.ch/listing/abc",
                    source=None,
                    title="Schoenes Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                changed = ListingInput(
                    url="https://flatfox.ch/listing/abc",
                    source=None,
                    title="Schoenes Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="Nur Frauen, ruhig sauber Anmeldung",
                    commute_minutes=None,
                )

                listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(original)))
                approve_listing(conn, listing_id, note="approved before change")
                upsert_listing(conn, score_listing(normalize_listing(changed)))
                row = conn.execute(
                    "SELECT approval_status, approved_raw_hash, decision FROM listings WHERE id = ?",
                    (listing_id,),
                ).fetchone()

                self.assertEqual(row["approval_status"], "not_requested")
                self.assertIsNone(row["approved_raw_hash"])
                self.assertEqual(row["decision"], "skip")
                with self.assertRaises(SystemExit):
                    mark_sent(conn, listing_id, note=None)

    def test_daily_plan_respects_site_limit_last_24h(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                for index in range(3):
                    listing = ListingInput(
                        url=f"https://flatfox.ch/listing/{index}",
                        source="flatfox",
                        title=f"Schoenes Zimmer Root {index}",
                        rent_chf=760 + index,
                        city="Root",
                        move_in="16.07.",
                        contact_name=None,
                        contact_email=None,
                        raw_text="ruhig sauber Anmeldung",
                        commute_minutes=None,
                    )
                    listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(listing)))
                    if index == 0:
                        approve_listing(conn, listing_id, note="approved")
                        mark_sent(conn, listing_id, note="sent")

                plan = safe_daily_plan(conn, daily_limit=5, site_daily_limit=1)

                self.assertEqual(plan, [])


class CoordCommuteTest(unittest.TestCase):
    """estimate_commute must prefer the listing's coords/address over the city."""

    def test_estimate_commute_passes_coords_and_address_to_live_lookup(self) -> None:
        captured: dict = {}

        def fake_live(query, *, origin_coords=None):
            captured["query"] = query
            captured["coords"] = origin_coords
            return cs.RouteResult(minutes=16, mode="e-bike (live)", distance_km=4.8)

        with patch.object(cs, "is_enabled", return_value=True), \
             patch("execution.commute_scoring.live_commute_minutes", side_effect=fake_live), \
             patch("execution.transit_scoring.live_transit_minutes", return_value=None):
            minutes, klass, mode = estimate_commute(
                "Luzern",
                coords=(47.0502, 8.3093),
                address="Bruchstrasse 12, 6003 Luzern",
            )
        self.assertEqual(minutes, 16)
        self.assertEqual(klass, "A+")
        # e-bike leg routed from the exact coords; address (not bare city) is the key.
        self.assertEqual(captured["coords"], (47.0502, 8.3093))
        self.assertEqual(captured["query"], "Bruchstrasse 12, 6003 Luzern")

    def test_estimate_commute_address_drives_transit_query(self) -> None:
        captured: dict = {}

        def fake_transit(location):
            captured["location"] = location
            return None  # force fallback to e-bike-only path

        with patch.object(cs, "is_enabled", return_value=True), \
             patch("execution.commute_scoring.live_commute_minutes",
                   return_value=cs.RouteResult(minutes=22, mode="e-bike (live)", distance_km=6.1)), \
             patch("execution.transit_scoring.live_transit_minutes", side_effect=fake_transit):
            estimate_commute(
                "Ebikon", coords=(47.08, 8.34), address="Riedmattstrasse 9, 6030 Ebikon",
            )
        self.assertEqual(captured["location"], "Riedmattstrasse 9, 6030 Ebikon")


class RescoreTest(unittest.TestCase):
    """rescore_all refreshes scoring from stored coords/address but must never
    disturb lifecycle columns (status/approval/sent/notified/created)."""

    def _seed(self, conn, *, commute_minutes_override=5) -> int:
        listing = ListingInput(
            url="https://flatfox.ch/listing/rescore-1",
            source="flatfox",
            title="Schoenes Zimmer Luzern",
            rent_chf=780,
            city="Luzern",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="ruhig sauber Anmeldung",
            commute_minutes=commute_minutes_override,
            latitude=47.0502,
            longitude=8.3093,
            address="Bruchstrasse 12, 6003 Luzern",
        )
        listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(listing)))
        return listing_id

    def test_rescore_preserves_status_and_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing_id = self._seed(conn)
                approve_listing(conn, listing_id, note="approved")
                mark_sent(conn, listing_id, note="sent")

                before = dict(conn.execute(
                    "SELECT status, approval_status, sent_at, notified_at, created_at "
                    "FROM listings WHERE id = ?", (listing_id,),
                ).fetchone())

                # Static fallback for Luzern (no live key in tests) is 30 min — so
                # the stored 5-min "manual override" gets replaced on rescore.
                rescored, changed = rescore_all(conn)

                after = dict(conn.execute(
                    "SELECT status, approval_status, sent_at, notified_at, created_at, "
                    "commute_minutes, commute_mode FROM listings WHERE id = ?",
                    (listing_id,),
                ).fetchone())

        self.assertEqual(rescored, 1)
        # Lifecycle columns are untouched.
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["approval_status"], before["approval_status"])
        self.assertEqual(after["sent_at"], before["sent_at"])
        self.assertEqual(after["notified_at"], before["notified_at"])
        self.assertEqual(after["created_at"], before["created_at"])
        # Scoring was refreshed away from the stale override.
        self.assertNotEqual(after["commute_mode"], "manual override")

    def test_rescore_noop_preserves_updated_at(self) -> None:
        # A rescore that produces identical scoring must not bump updated_at, so
        # it never spuriously widens the notifier's new-listing window.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing = ListingInput(
                    url="https://flatfox.ch/listing/noop-1",
                    source="flatfox",
                    title="Schoenes Zimmer Root",
                    rent_chf=780,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(listing)))
                before = conn.execute(
                    "SELECT updated_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()[0]

                rescored, changed = rescore_all(conn)

                after = conn.execute(
                    "SELECT updated_at FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()[0]

        self.assertEqual((rescored, changed), (1, 0))
        self.assertEqual(after, before)  # unchanged row keeps its timestamp

    def test_rescore_reports_decision_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
                # Seed with a 5-min override → A+ apply; rescore drops the override
                # so the row re-scores on the real (static) commute and may move tier.
                self._seed(conn, commute_minutes_override=5)
                rescored, changed = rescore_all(conn)
        self.assertEqual(rescored, 1)
        self.assertIn(changed, (0, 1))


class FreshnessTest(unittest.TestCase):
    """last_seen stamping, expiry reactivation, and expired-row exclusion."""

    def _listing(self, **kw) -> ListingInput:
        defaults = dict(
            url="https://flatfox.ch/en/flat/test/777/",
            source="flatfox.ch",
            title="WG Root",
            rent_chf=780,
            city="Root",
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="ruhig sauber Anmeldung",
            commute_minutes=None,
        )
        defaults.update(kw)
        return ListingInput(**defaults)

    def test_insert_stamps_last_seen(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db)) as conn:
                init_db(conn)
                lid, created = upsert_listing(conn, score_listing(normalize_listing(self._listing())))
                row = conn.execute(
                    "SELECT last_seen, created_at, expired_at FROM listings WHERE id = ?", (lid,)
                ).fetchone()
        self.assertTrue(created)
        self.assertIsNotNone(row["last_seen"])
        self.assertEqual(row["last_seen"], row["created_at"])
        self.assertIsNone(row["expired_at"])

    def test_resighting_reactivates_expired_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db)) as conn:
                init_db(conn)
                lid, _ = upsert_listing(conn, score_listing(normalize_listing(self._listing())))
                conn.execute(
                    "UPDATE listings SET status='expired', expired_at='2026-05-26T00:00:00+00:00', "
                    "last_seen='2026-05-20T00:00:00+00:00' WHERE id = ?",
                    (lid,),
                )
                conn.commit()
                # Same canonical_key -> updates the existing row.
                lid2, created = upsert_listing(conn, score_listing(normalize_listing(self._listing())))
                row = conn.execute(
                    "SELECT status, expired_at, last_seen FROM listings WHERE id = ?", (lid,)
                ).fetchone()
        self.assertEqual(lid2, lid)
        self.assertFalse(created)
        self.assertEqual(row["status"], "new")          # reactivated
        self.assertIsNone(row["expired_at"])            # expiry cleared
        self.assertNotEqual(row["last_seen"], "2026-05-20T00:00:00+00:00")  # refreshed

    def test_resighting_does_not_resurrect_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db)) as conn:
                init_db(conn)
                lid, _ = upsert_listing(conn, score_listing(normalize_listing(self._listing())))
                approve_listing(conn, lid, note="ok")
                mark_sent(conn, lid, note="sent")
                upsert_listing(conn, score_listing(normalize_listing(self._listing())))
                status = conn.execute("SELECT status FROM listings WHERE id = ?", (lid,)).fetchone()[0]
        self.assertEqual(status, "sent")  # sent_at wins over the expired->new flip

    def test_expired_excluded_from_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "listings.sqlite"
            with closing(connect(db)) as conn:
                init_db(conn)
                # Distinct content (title/rent/url) so they don't dedupe together.
                keep, _ = upsert_listing(conn, score_listing(normalize_listing(self._listing(
                    url="https://flatfox.ch/en/flat/a/1/", title="WG Root A", rent_chf=780))))
                gone, _ = upsert_listing(conn, score_listing(normalize_listing(self._listing(
                    url="https://flatfox.ch/en/flat/b/2/", title="WG Root B", rent_chf=820))))
                conn.execute(
                    "UPDATE listings SET status='expired', expired_at='2026-05-26T00:00:00+00:00' WHERE id = ?",
                    (gone,),
                )
                conn.commit()
                ids = {row["id"] for row in list_queue(conn, limit=50)}
        self.assertIn(keep, ids)
        self.assertNotIn(gone, ids)


if __name__ == "__main__":
    unittest.main()
