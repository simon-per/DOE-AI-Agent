import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from execution.apartment_pipeline import (
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    score_listing,
    upsert_listing,
)
from execution.apartment_workflow import BASE_DIR, main
from execution.flatfox_public_sync import SyncStats


def write_sources(path: Path) -> None:
    path.write_text(
        "source,display_name,base_url,priority,coverage,ingestion_method,automation_status,notes\n"
        "flatfox.ch,Flatfox,https://flatfox.ch,high,WG,public_api,implemented,test\n",
        encoding="utf-8",
    )


def fake_flatfox_sync(args) -> SyncStats:
    with closing(connect(args.db)) as conn:
        init_db(conn)
        listing = ListingInput(
            url="https://flatfox.ch/en/flat/root/1/",
            source="flatfox.ch",
            title="WG Zimmer Root",
            rent_chf=760,
            city="Root",
            move_in="2026-07-16",
            contact_name=None,
            contact_email=None,
            raw_text="ruhig sauber Anmeldung Veloplatz",
            commute_minutes=None,
        )
        upsert_listing(conn, score_listing(normalize_listing(listing)))
    return SyncStats(fetched=1, matched=1, created=1, skipped=0, pages=1)


class ApartmentWorkflowTest(unittest.TestCase):
    def test_live_workflow_ingests_exports_without_sending(self) -> None:
        tmp_root = BASE_DIR / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            csv_path = tmp_path / "queue.csv"
            drafts_path = tmp_path / "drafts.md"
            write_sources(sources_path)

            with patch("execution.apartment_workflow.sync_flatfox_public", side_effect=fake_flatfox_sync):
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "--sources",
                        str(sources_path),
                        "--csv",
                        str(csv_path),
                        "--drafts",
                        str(drafts_path),
                        "--skip-emails",
                        "--skip-google",
                        "--skip-daily-plan",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertTrue(db_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertTrue(drafts_path.exists())
            self.assertIn("WG Zimmer Root", drafts_path.read_text(encoding="utf-8"))

    def test_dry_run_uses_temp_paths_and_google_dry_run(self) -> None:
        tmp_root = BASE_DIR / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            csv_path = tmp_path / "queue.csv"
            drafts_path = tmp_path / "drafts.md"
            write_sources(sources_path)

            with patch("execution.apartment_workflow.sync_flatfox_public", side_effect=fake_flatfox_sync), \
                patch("execution.apartment_workflow.google_sheets_main", return_value=0) as google_sync:
                result = main(
                    [
                        "--dry-run",
                        "--db",
                        str(db_path),
                        "--sources",
                        str(sources_path),
                        "--csv",
                        str(csv_path),
                        "--drafts",
                        str(drafts_path),
                        "--skip-emails",
                        "--skip-daily-plan",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertFalse(db_path.exists())
            self.assertFalse(csv_path.exists())
            self.assertFalse(drafts_path.exists())
            google_args = google_sync.call_args.args[0]
            self.assertIn("--dry-run", google_args)
            self.assertIn("--db", google_args)

    def test_live_workflow_calls_google_sync_with_expected_args(self) -> None:
        tmp_root = BASE_DIR / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            write_sources(sources_path)

            with patch("execution.apartment_workflow.sync_flatfox_public", side_effect=fake_flatfox_sync), \
                patch("execution.apartment_workflow.google_sheets_main", return_value=0) as google_sync:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "--sources",
                        str(sources_path),
                        "--skip-emails",
                        "--skip-export",
                        "--skip-daily-plan",
                        "--sheet-name",
                        "Swiss Appartment Search Pipeline",
                        "--spreadsheet-id",
                        "sheet-id",
                        "--create-sheet-if-missing",
                    ]
                )

            self.assertEqual(result, 0)
            google_args = google_sync.call_args.args[0]
            self.assertNotIn("--dry-run", google_args)
            self.assertIn("--db", google_args)
            self.assertIn(str(db_path), google_args)
            self.assertIn("--sources", google_args)
            self.assertIn(str(sources_path), google_args)
            self.assertIn("--spreadsheet-id", google_args)
            self.assertIn("sheet-id", google_args)
            self.assertIn("--create-if-missing", google_args)

    def test_rejects_writable_paths_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outside_db = Path(tmpdir) / "listings.sqlite"

            with self.assertRaises(SystemExit) as ctx:
                main(["--db", str(outside_db), "--skip-flatfox", "--skip-emails", "--skip-google"])

            self.assertIn("must be inside this repository", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
