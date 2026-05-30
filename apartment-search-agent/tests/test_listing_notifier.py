import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from execution import listing_notifier as ln
from execution.apartment_pipeline import connect, init_db


def _iso_days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _insert(
    conn,
    *,
    key,
    decision="apply",
    created_at=None,
    updated_at=None,
    notified_at=None,
    priority_score=80,
    city="Luzern",
    rent=850,
    url=None,
    commute_class="A",
    commute_minutes=18,
    commute_mode="oeV (live)",
    title="WG in Luzern",
    move_in="2026-07-16",
    message_draft="Hoi zaeme, ich interessiere mich fuer das Zimmer.",
    contact_email=None,
):
    created_at = created_at or _iso_days_ago(0)
    updated_at = updated_at or created_at
    url = url or ("https://x.test/" + key)
    conn.execute(
        """
        INSERT INTO listings (
            canonical_key, source, title, rent_chf, city, url, move_in,
            raw_hash, decision, recommended_action, priority_score,
            commute_class, commute_minutes, commute_mode, price_score,
            wg_fit_score, gender_status, scam_risk, flags_json,
            message_variant, message_draft, status, approval_status,
            contact_email, created_at, updated_at, notified_at
        ) VALUES (?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?)
        """,
        (
            key, "flatfox.ch", title, rent, city, url, move_in,
            "hash-" + key, decision, "Apply after approval", priority_score,
            commute_class, commute_minutes, commute_mode, "good",
            60, "eligible", "low", "[]",
            "v1", message_draft, "new", "not_requested",
            contact_email, created_at, updated_at, notified_at,
        ),
    )
    conn.commit()


class ParseSinceTest(unittest.TestCase):
    def test_units_and_bare_day_count(self) -> None:
        now = datetime.now(UTC)
        self.assertAlmostEqual((now - ln.parse_since("7d")).total_seconds(), 7 * 86400, delta=5)
        self.assertAlmostEqual((now - ln.parse_since("48h")).total_seconds(), 48 * 3600, delta=5)
        self.assertAlmostEqual((now - ln.parse_since("2w")).total_seconds(), 14 * 86400, delta=5)
        self.assertAlmostEqual((now - ln.parse_since("3")).total_seconds(), 3 * 86400, delta=5)

    def test_invalid_raises(self) -> None:
        with self.assertRaises(ValueError):
            ln.parse_since("soon")


class _DbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=ln.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "listings.sqlite"
        with closing(connect(self.db)) as conn:
            init_db(conn)

    def _notified_at(self, key: str):
        with closing(connect(self.db)) as conn:
            row = conn.execute(
                "SELECT notified_at FROM listings WHERE canonical_key=?", (key,)
            ).fetchone()
        return row["notified_at"] if row else None


class NotifySelectionTest(_DbTest):
    def test_empty_db_no_candidates(self) -> None:
        result = ln.notify_new_listings(self.db, dry_run=True)
        self.assertEqual(result.candidates, 0)
        self.assertFalse(result.sent)

    def test_only_apply_decision_counts(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="apply1", decision="apply")
            _insert(conn, key="consider1", decision="consider")
            _insert(conn, key="skip1", decision="skip")
        result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 1)

    def test_already_notified_rows_excluded(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="old", notified_at=_iso_days_ago(0))
            _insert(conn, key="new")
        result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 1)

    def test_since_window_excludes_old(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="recent", created_at=_iso_days_ago(1))
            _insert(conn, key="ancient", created_at=_iso_days_ago(30))
        result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 1)

    def test_already_actioned_status_excluded(self) -> None:
        # A rescore bumps updated_at on apply rows; one already marked 'sent'
        # must not resurface in the brief (mirrors select_candidates/queue_rows).
        with closing(connect(self.db)) as conn:
            _insert(conn, key="apply-sent")
            conn.execute(
                "UPDATE listings SET status='sent' WHERE canonical_key=?", ("apply-sent",)
            )
            conn.commit()
            _insert(conn, key="apply-new")
        result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 1)

    def test_promoted_listing_via_updated_at_window(self) -> None:
        # Reviewer H1: a row created 30d ago (outside the 7d window) but
        # re-scored/promoted to apply today (fresh updated_at) must still surface.
        with closing(connect(self.db)) as conn:
            _insert(
                conn, key="promoted",
                created_at=_iso_days_ago(30), updated_at=_iso_days_ago(0),
            )
        result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 1)


class NotifySendTest(_DbTest):
    def test_dry_run_reports_without_loading_sender(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
            _insert(conn, key="b")
        with patch.object(ln, "_load_send_email") as loader:
            result = ln.notify_new_listings(self.db, since="7d", dry_run=True)
        self.assertEqual(result.candidates, 2)
        self.assertFalse(result.sent)
        loader.assert_not_called()
        self.assertIsNone(self._notified_at("a"))
        self.assertIsNone(self._notified_at("b"))

    def test_send_calls_once_and_stamps_on_success(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
            _insert(conn, key="b")
        sender = MagicMock(return_value=True)
        with patch.object(ln, "_load_send_email", return_value=sender):
            result = ln.notify_new_listings(
                self.db, since="7d", dry_run=False, recipient="me@test.ch"
            )
        self.assertTrue(result.sent)
        self.assertEqual(result.candidates, 2)
        sender.assert_called_once()
        subject, body, to = sender.call_args.args
        html_body = sender.call_args.kwargs.get("html")
        self.assertIn("2 new apply-tier", subject)
        self.assertEqual(to, "me@test.ch")
        self.assertIn("WG in Luzern", body)
        self.assertIn("ich interessiere mich", body)  # the ready-to-send draft
        self.assertIsNotNone(html_body)
        self.assertIn("<pre", html_body)
        self.assertIsNotNone(self._notified_at("a"))
        self.assertIsNotNone(self._notified_at("b"))

    def test_failed_send_does_not_stamp(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
        sender = MagicMock(return_value=False)
        with patch.object(ln, "_load_send_email", return_value=sender):
            result = ln.notify_new_listings(self.db, dry_run=False, recipient="me@test.ch")
        self.assertFalse(result.sent)
        self.assertIn("returned False", result.reason)
        self.assertIsNone(self._notified_at("a"))

    def test_noop_when_send_email_unimportable(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
        with patch.object(ln, "_load_send_email", return_value=None):
            result = ln.notify_new_listings(self.db, dry_run=False, recipient="me@test.ch")
        self.assertFalse(result.sent)
        self.assertIn("unavailable", result.reason)
        self.assertIsNone(self._notified_at("a"))

    def test_recipient_defaults_to_env(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
        sender = MagicMock(return_value=True)
        with patch.object(ln, "_load_send_email", return_value=sender), \
             patch.dict("os.environ", {"APARTMENT_NOTIFY_TO": "fallback@test.ch"}, clear=False):
            result = ln.notify_new_listings(self.db, dry_run=False)
        self.assertTrue(result.sent)
        _, _, to = sender.call_args.args
        self.assertEqual(to, "fallback@test.ch")


class ActionPacketTest(_DbTest):
    def _render(self):
        from execution.listing_notifier import fetch_candidates, format_summary
        with closing(connect(self.db)) as conn:
            rows = fetch_candidates(conn, "7d", 25)
        return format_summary(rows)

    def test_packet_has_draft_apply_link_commute_and_wgfit(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(
                conn, key="a", url="https://flatfox.ch/x/123",
                commute_minutes=11, commute_mode="oeV 11 / e-bike 35 (live)",
                message_draft="Hoi zaeme, das Zimmer passt super.",
            )
        subject, text_body, html_body = self._render()
        self.assertIn("1 new apply-tier listing", subject)
        self.assertIn("oeV 11 / e-bike 35 (live)", text_body)
        self.assertIn("WG-fit 60", text_body)
        self.assertIn("https://flatfox.ch/x/123", text_body)
        self.assertIn("Hoi zaeme, das Zimmer passt super.", text_body)
        self.assertIn('href="https://flatfox.ch/x/123"', html_body)
        self.assertIn("<pre", html_body)

    def test_html_escapes_draft(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a", message_draft="<script>alert(1)</script>")
        _, _, html_body = self._render()
        self.assertNotIn("<script>", html_body)
        self.assertIn("&lt;script&gt;", html_body)

    def test_contact_email_surfaced_when_present(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a", contact_email="anna@example.ch")
        _, text_body, html_body = self._render()
        self.assertIn("anna@example.ch", text_body)
        self.assertIn("mailto:anna@example.ch", html_body)

    def test_llm_line_shown_when_scored(self) -> None:
        # Advisory re-rank score surfaces as an LLM line in both text and HTML.
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
            conn.execute(
                "UPDATE listings SET openrouter_score=?, openrouter_reason=? WHERE canonical_key=?",
                (88, "calm WG, close to Root", "a"),
            )
            conn.commit()
        _, text_body, html_body = self._render()
        self.assertIn("LLM 88 — calm WG, close to Root", text_body)
        self.assertIn("LLM 88", html_body)

    def test_no_llm_line_when_unscored(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
        _, text_body, _ = self._render()
        self.assertNotIn("LLM ", text_body)


class CliTest(_DbTest):
    def test_main_dry_run_default_makes_no_send(self) -> None:
        with closing(connect(self.db)) as conn:
            _insert(conn, key="a")
        with patch.object(ln, "_load_send_email") as loader:
            rc = ln.main(["--db", str(self.db), "--since", "7d"])
        self.assertEqual(rc, 0)
        loader.assert_not_called()

    def test_preview_prints_body_without_sending(self) -> None:
        import io
        from contextlib import redirect_stdout

        with closing(connect(self.db)) as conn:
            _insert(conn, key="a", message_draft="Hoi zaeme, preview test.")
        buf = io.StringIO()
        with patch.object(ln, "_load_send_email") as loader, redirect_stdout(buf):
            rc = ln.main(["--db", str(self.db), "--since", "7d", "--preview"])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        loader.assert_not_called()
        self.assertIn("Subject:", out)
        self.assertIn("Hoi zaeme, preview test.", out)


class WorkflowSkipNotifyTest(unittest.TestCase):
    def _run(self, *extra):
        from execution.apartment_workflow import BASE_DIR as WF_BASE
        from execution.apartment_workflow import main as wf_main

        tmp_root = WF_BASE / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as d:
            p = Path(d)
            (p / "sources.csv").write_text(
                "source,display_name,base_url,priority,coverage,ingestion_method,automation_status,notes\n"
                "flatfox.ch,Flatfox,https://flatfox.ch,high,WG,public_api,implemented,t\n",
                encoding="utf-8",
            )
            args = [
                "--db", str(p / "listings.sqlite"),
                "--sources", str(p / "sources.csv"),
                "--csv", str(p / "q.csv"),
                "--drafts", str(p / "d.md"),
                "--skip-flatfox", "--skip-emails", "--skip-google", "--skip-daily-plan",
                *extra,
            ]
            with patch(
                "execution.listing_notifier.notify_new_listings",
                return_value=ln.NotifyResult(0, False, reason="patched"),
            ) as notify:
                rc = wf_main(args)
        return rc, notify

    def test_skip_notify_makes_zero_calls(self) -> None:
        rc, notify = self._run("--skip-notify")
        self.assertEqual(rc, 0)
        notify.assert_not_called()

    def test_notify_runs_by_default(self) -> None:
        rc, notify = self._run()
        self.assertEqual(rc, 0)
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
