import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from execution.apartment_pipeline import (
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    score_listing,
    upsert_listing,
)
from execution.google_sheets_sync import (
    APPLICATIONS_SHEET,
    LISTINGS_SHEET,
    PIPELINE_SHEET,
    QUEUE_SHEET,
    SOURCES_SHEET,
    SUMMARY_SHEET,
    SETTINGS_SHEET,
    cleanup_worksheets_gspread,
    enrich_listing_row,
    load_google_client,
    build_workbook_values,
    load_google_service,
    main,
    merge_application_values,
    merge_pipeline_values,
    open_google_spreadsheet,
    require_repo_local_path,
    source_rows,
    sync_values_gspread,
    sheets_api_call,
    sheet_values_are_blank,
    token_file_has_required_scopes,
)


class GoogleSheetsSyncTest(unittest.TestCase):
    def test_build_workbook_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            sources_path.write_text(
                "source,display_name,base_url,priority,coverage,ingestion_method,automation_status,notes\n"
                "flatfox.ch,Flatfox,https://flatfox.ch,high,WG,public_api,implemented,test\n",
                encoding="utf-8",
            )

            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing = ListingInput(
                    url="https://flatfox.ch/en/flat/example/1/",
                    source="flatfox.ch",
                    title="WG Zimmer Root",
                    rent_chf=760,
                    city="Root",
                    move_in="16.07.",
                    contact_name=None,
                    contact_email=None,
                    raw_text="ruhig sauber Anmeldung",
                    commute_minutes=None,
                )
                upsert_listing(conn, score_listing(normalize_listing(listing)))
                workbook = build_workbook_values(conn, sources_path)

            self.assertEqual(
                set(workbook),
                {PIPELINE_SHEET, SOURCES_SHEET, SUMMARY_SHEET, SETTINGS_SHEET},
            )
            self.assertGreaterEqual(len(workbook[PIPELINE_SHEET]), 2)
            self.assertEqual(workbook[SOURCES_SHEET][1][0], "flatfox.ch")
            self.assertIn("status", workbook[PIPELINE_SHEET][0])
            self.assertIn("system_decision", workbook[PIPELINE_SHEET][0])
            self.assertIn("final_message", workbook[PIPELINE_SHEET][0])
            self.assertEqual(workbook[SUMMARY_SHEET][0], ["metric", "value"])

    def test_enrich_is_active_and_last_seen_reflect_freshness(self) -> None:
        live = enrich_listing_row({
            "id": 1, "status": "new", "sent_at": None, "decision": "consider",
            "created_at": "2026-05-28T00:00:00+00:00",
            "last_seen": "2026-05-30T00:00:00+00:00",
        })
        self.assertEqual(live["is_active"], "yes")
        self.assertEqual(live["last_seen"], "2026-05-30T00:00:00+00:00")

        expired = enrich_listing_row({
            "id": 2, "status": "expired", "sent_at": None, "decision": "consider",
            "created_at": "2026-05-28T00:00:00+00:00",
            "expired_at": "2026-05-29T00:00:00+00:00",
            "last_seen": "2026-05-28T00:00:00+00:00",
        })
        self.assertEqual(expired["is_active"], "no")  # expired -> not active
        self.assertEqual(expired["first_seen"], "2026-05-28T00:00:00+00:00")

    def test_dry_run_does_not_load_google_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            sources_path.write_text(
                "source,display_name,base_url,priority,coverage,ingestion_method,automation_status,notes\n"
                "flatfox.ch,Flatfox,https://flatfox.ch,high,WG,public_api,implemented,test\n",
                encoding="utf-8",
            )

            with closing(connect(db_path)) as conn:
                init_db(conn)

            with patch("execution.google_sheets_sync.load_google_client") as google_client:
                result = main(["--db", str(db_path), "--sources", str(sources_path), "--dry-run"])

            self.assertEqual(result, 0)
            google_client.assert_not_called()

    def test_source_registry_requires_expected_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sources_path = Path(tmpdir) / "source_registry.csv"
            sources_path.write_text("source,display_name\nflatfox.ch,Flatfox\n", encoding="utf-8")

            with self.assertRaises(SystemExit) as ctx:
                source_rows(sources_path)

            self.assertIn("missing required columns", str(ctx.exception))

    def test_missing_google_credentials_has_clear_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            load_google_service(Path("does-not-exist.json"))

        self.assertIn("Google credentials file not found", str(ctx.exception))

    def test_missing_oauth_credentials_has_clear_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            load_google_client(Path("does-not-exist.json"), Path("token.json"))

        self.assertIn("Google OAuth credentials file not found", str(ctx.exception))

    def test_oauth_token_path_must_be_repo_local(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            require_repo_local_path(Path(__file__).resolve().parents[2] / "token.json", "Google OAuth token path")

        self.assertIn("must be inside this repository", str(ctx.exception))

    def test_load_google_client_uses_existing_oauth_token(self) -> None:
        class FakeCredentials:
            valid = True
            expired = False
            refresh_token = None

            @classmethod
            def from_authorized_user_file(cls, path: str, scopes: list[str]):
                self.assertTrue(path.endswith("token.json"))
                self.assertIn("https://www.googleapis.com/auth/spreadsheets", scopes)
                return cls()

            def has_scopes(self, scopes: list[str]) -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            credentials_path = tmp_path / "credentials.json"
            token_path = tmp_path / "token.json"
            credentials_path.write_text("{}", encoding="utf-8")
            token_path.write_text(
                "{\"scopes\": ["
                "\"https://www.googleapis.com/auth/spreadsheets\", "
                "\"https://www.googleapis.com/auth/drive.file\""
                "]}",
                encoding="utf-8",
            )
            fake_gspread = MagicMock()
            fake_gspread.authorize.return_value = "client"

            with patch(
                "execution.google_sheets_sync.import_oauth_libraries",
                return_value=(fake_gspread, object, FakeCredentials, object),
            ):
                client = load_google_client(credentials_path, token_path)

            self.assertEqual(client, "client")
            fake_gspread.authorize.assert_called_once()

    def test_token_file_scope_validation_uses_granted_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = Path(tmpdir) / "token.json"
            token_path.write_text(
                "{\"scopes\": [\"https://www.googleapis.com/auth/spreadsheets\"]}",
                encoding="utf-8",
            )

            self.assertFalse(token_file_has_required_scopes(token_path))

            token_path.write_text(
                "{\"scope\": \"https://www.googleapis.com/auth/spreadsheets "
                "https://www.googleapis.com/auth/drive.file\"}",
                encoding="utf-8",
            )

            self.assertTrue(token_file_has_required_scopes(token_path))

    def test_load_google_client_reruns_oauth_when_existing_token_lacks_scope(self) -> None:
        class FakeCredentials:
            valid = True
            expired = False
            refresh_token = None

            @classmethod
            def from_authorized_user_file(cls, path: str, scopes: list[str]):
                return cls()

            def has_scopes(self, scopes: list[str]) -> bool:
                return False

        class FakeFlow:
            def run_local_server(self, port: int):
                refreshed = MagicMock()
                refreshed.to_json.return_value = "{\"token\": \"new\"}"
                return refreshed

        fake_installed_flow = MagicMock()
        fake_installed_flow.from_client_secrets_file.return_value = FakeFlow()
        fake_gspread = MagicMock()
        fake_gspread.authorize.return_value = "client"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            credentials_path = tmp_path / "credentials.json"
            token_path = tmp_path / "token.json"
            credentials_path.write_text("{}", encoding="utf-8")
            token_path.write_text("{}", encoding="utf-8")

            with patch(
                "execution.google_sheets_sync.import_oauth_libraries",
                return_value=(fake_gspread, object, FakeCredentials, fake_installed_flow),
            ):
                client = load_google_client(credentials_path, token_path)

            self.assertEqual(client, "client")
            fake_installed_flow.from_client_secrets_file.assert_called_once()
            self.assertEqual(token_path.read_text(encoding="utf-8"), "{\"token\": \"new\"}")

    def test_sheets_api_call_retries_gspread_api_errors_with_resp_status(self) -> None:
        class Response:
            status = 503

        class RetryableError(Exception):
            resp = Response()

        calls = {"count": 0}

        def flaky_call():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RetryableError("retry")
            return "ok"

        with patch("execution.google_sheets_sync.time.sleep") as sleep:
            result = sheets_api_call(flaky_call)

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once_with(1)

    def test_merge_application_values_preserves_manual_columns(self) -> None:
        generated = [
            ["listing_id", "active_in_queue", "title", "application_status", "final_message", "response_notes"],
            [1, "yes", "New title", "", "", ""],
            [2, "yes", "Other", "", "", ""],
        ]
        existing = [
            ["listing_id", "active_in_queue", "title", "application_status", "final_message", "response_notes"],
            ["1", "yes", "Old title", "sent", "Custom message", "Viewing booked"],
        ]

        merged = merge_application_values(generated, existing)

        self.assertEqual(merged[1], [1, "yes", "New title", "sent", "Custom message", "Viewing booked"])
        self.assertEqual(merged[2], [2, "yes", "Other", "", "", ""])

    def test_merge_application_values_preserves_rows_that_leave_queue(self) -> None:
        generated = [
            ["listing_id", "active_in_queue", "title", "application_status", "final_message"],
            [2, "yes", "Current listing", "", ""],
        ]
        existing = [
            ["listing_id", "active_in_queue", "title", "application_status", "final_message"],
            ["1", "yes", "Old sent listing", "sent", "Custom sent message"],
            ["2", "yes", "Current old title", "drafted", "Edited current"],
        ]

        merged = merge_application_values(generated, existing)

        self.assertEqual(merged[1], [2, "yes", "Current listing", "drafted", "Edited current"])
        self.assertEqual(merged[2], ["1", "no", "Old sent listing", "sent", "Custom sent message"])

    def test_merge_pipeline_values_migrates_application_manual_fields(self) -> None:
        generated = [
            ["listing_id", "status", "title", "system_decision", "final_message", "response_notes"],
            [1, "new", "Fresh title", "apply", "", ""],
        ]
        existing_applications = [
            ["listing_id", "application_status", "final_message", "response_notes"],
            ["1", "sent", "Custom migrated message", "Viewing next week"],
        ]

        merged = merge_pipeline_values(generated, existing_pipeline_values=None, existing_application_values=existing_applications)

        self.assertEqual(
            merged[1],
            [1, "sent", "Fresh title", "apply", "Custom migrated message", "Viewing next week"],
        )

    def test_merge_pipeline_values_prefers_existing_pipeline_manual_fields(self) -> None:
        generated = [
            ["listing_id", "status", "title", "system_decision", "final_message"],
            [1, "new", "Fresh title", "apply", ""],
        ]
        existing_pipeline = [
            ["listing_id", "status", "final_message"],
            ["1", "viewing", "Pipeline message"],
        ]
        existing_applications = [
            ["listing_id", "application_status", "final_message"],
            ["1", "sent", "Old application message"],
        ]

        merged = merge_pipeline_values(generated, existing_pipeline, existing_applications)

        self.assertEqual(merged[1], [1, "viewing", "Fresh title", "apply", "Pipeline message"])

    def test_open_google_spreadsheet_prefers_id(self) -> None:
        client = MagicMock()
        client.open_by_key.return_value = "spreadsheet"

        spreadsheet = open_google_spreadsheet(
            client,
            spreadsheet_id="sheet-id",
            sheet_name="Swiss Appartment Search Pipeline",
        )

        self.assertEqual(spreadsheet, "spreadsheet")
        client.open_by_key.assert_called_once_with("sheet-id")
        client.open.assert_not_called()

    def test_open_google_spreadsheet_by_name(self) -> None:
        client = MagicMock()
        client.open.return_value = "spreadsheet"

        spreadsheet = open_google_spreadsheet(
            client,
            spreadsheet_id=None,
            sheet_name="Swiss Appartment Search Pipeline",
        )

        self.assertEqual(spreadsheet, "spreadsheet")
        client.open.assert_called_once_with("Swiss Appartment Search Pipeline")

    def test_open_google_spreadsheet_can_create_missing_sheet(self) -> None:
        class SpreadsheetNotFound(Exception):
            pass

        client = MagicMock()
        client.open.side_effect = SpreadsheetNotFound()
        client.create.return_value = "new-spreadsheet"

        spreadsheet = open_google_spreadsheet(
            client,
            spreadsheet_id=None,
            sheet_name="Swiss Appartment Search Pipeline",
            create_if_missing=True,
        )

        self.assertEqual(spreadsheet, "new-spreadsheet")
        client.create.assert_called_once_with("Swiss Appartment Search Pipeline")

    def test_open_google_spreadsheet_reports_missing_id_when_id_was_used(self) -> None:
        class SpreadsheetNotFound(Exception):
            pass

        client = MagicMock()
        client.open_by_key.side_effect = SpreadsheetNotFound()

        with self.assertRaises(SystemExit) as ctx:
            open_google_spreadsheet(
                client,
                spreadsheet_id="bad-id",
                sheet_name="Swiss Appartment Search Pipeline",
            )

        self.assertIn("bad-id", str(ctx.exception))

    def test_sync_values_gspread_updates_and_clears_generated_range(self) -> None:
        spreadsheet = MagicMock()
        worksheet = MagicMock()
        worksheet.row_count = 10
        worksheet.col_count = 4
        spreadsheet.worksheet.return_value = worksheet
        spreadsheet.worksheets.return_value = [worksheet]
        worksheet.title = "Listings"

        sync_values_gspread(spreadsheet, {"Listings": [["a", "b"], ["1", "2"]]})

        spreadsheet.worksheet.assert_called_once_with("Listings")
        worksheet.update.assert_called_once_with(
            range_name="A1",
            values=[["a", "b"], ["1", "2"]],
            value_input_option="RAW",
        )
        worksheet.batch_clear.assert_called_once_with(["A3:D10000", "C1:D2"])

    def test_cleanup_removes_blank_default_sheet_and_orders_generated_tabs(self) -> None:
        spreadsheet = MagicMock()
        blank = MagicMock()
        blank.title = "Sheet1"
        blank.get_all_values.return_value = []
        pipeline = MagicMock()
        pipeline.title = "Pipeline"
        summary = MagicMock()
        summary.title = "Summary"
        spreadsheet.worksheets.return_value = [blank, summary, pipeline]

        cleanup_worksheets_gspread(
            spreadsheet,
            {
                "Summary": [["metric", "value"]],
                "Pipeline": [["listing_id"]],
            },
        )

        spreadsheet.del_worksheet.assert_called_once_with(blank)
        spreadsheet.reorder_worksheets.assert_called_once_with([pipeline, summary])

    def test_cleanup_removes_legacy_generated_tabs_when_pipeline_exists(self) -> None:
        spreadsheet = MagicMock()
        pipeline = MagicMock()
        pipeline.title = "Pipeline"
        applications = MagicMock()
        applications.title = "Applications"
        listings = MagicMock()
        listings.title = "Listings"
        spreadsheet.worksheets.return_value = [applications, listings, pipeline]

        cleanup_worksheets_gspread(spreadsheet, {"Pipeline": [["listing_id"]]})

        spreadsheet.del_worksheet.assert_any_call(applications)
        spreadsheet.del_worksheet.assert_any_call(listings)

    def test_cleanup_keeps_nonempty_default_sheet(self) -> None:
        spreadsheet = MagicMock()
        default = MagicMock()
        default.title = "Sheet1"
        default.get_all_values.return_value = [["manual"]]
        listings = MagicMock()
        listings.title = "Listings"
        spreadsheet.worksheets.return_value = [default, listings]

        cleanup_worksheets_gspread(spreadsheet, {"Listings": [["listing_id"]]})

        spreadsheet.del_worksheet.assert_not_called()

    def test_sheet_values_are_blank_handles_empty_row(self) -> None:
        self.assertTrue(sheet_values_are_blank([]))
        self.assertTrue(sheet_values_are_blank([[]]))
        self.assertTrue(sheet_values_are_blank([["", " "]]))
        self.assertFalse(sheet_values_are_blank([["manual"]]))

    def test_explicit_service_account_is_not_shadowed_by_oauth_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "listings.sqlite"
            sources_path = tmp_path / "source_registry.csv"
            service_account_path = tmp_path / "service-account.json"
            sources_path.write_text(
                "source,display_name,base_url,priority,coverage,ingestion_method,automation_status,notes\n"
                "flatfox.ch,Flatfox,https://flatfox.ch,high,WG,public_api,implemented,test\n",
                encoding="utf-8",
            )
            service_account_path.write_text("{}", encoding="utf-8")

            with closing(connect(db_path)) as conn:
                init_db(conn)

            with patch("execution.google_sheets_sync.load_google_client") as oauth_client, \
                patch("execution.google_sheets_sync.load_google_service", return_value="service") as service_loader, \
                patch("execution.google_sheets_sync.merge_existing_manual_values_service") as merge_manual, \
                patch("execution.google_sheets_sync.sync_values") as sync_values:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "--sources",
                        str(sources_path),
                        "--service-account-credentials",
                        str(service_account_path),
                        "--spreadsheet-id",
                        "sheet-id",
                    ]
                )

            self.assertEqual(result, 0)
            oauth_client.assert_not_called()
            service_loader.assert_called_once_with(service_account_path)
            merge_manual.assert_called_once()
            sync_values.assert_called_once()


if __name__ == "__main__":
    unittest.main()
