import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from execution.apartment_pipeline import (
    ListingInput,
    approve_listing,
    canonicalize_url,
    connect,
    extract_rent,
    init_db,
    mark_sent,
    normalize_listing,
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


if __name__ == "__main__":
    unittest.main()
