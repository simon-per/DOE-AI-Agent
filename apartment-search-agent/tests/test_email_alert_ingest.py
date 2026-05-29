import argparse
import sqlite3
import tempfile
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from execution import email_alert_ingest as ingest
from execution.apartment_pipeline import connect, init_db


def make_email(
    *,
    sender: str,
    subject: str,
    plain: str = "",
    html: str = "",
    message_id: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or f"<{abs(hash((sender, subject)))}@fixture>"
    if html:
        if plain:
            msg.set_content(plain)
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content("", subtype="plain")
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(plain or "")
    return msg


class FakeIMAP:
    """Stand-in for imaplib.IMAP4_SSL used by the ingester."""

    def __init__(self, messages: list[EmailMessage]) -> None:
        self._messages = messages
        self.selected_readonly: bool | None = None
        self.logged_out = False

    def login(self, address: str, password: str) -> None:
        self.address = address
        self.password = password

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def search(self, charset, *terms) -> tuple[str, list[bytes]]:
        # We deliberately return everything regardless of FROM/SINCE — the test
        # asserts the ingester properly filters via parser_for_sender.
        ids = b" ".join(str(i + 1).encode() for i in range(len(self._messages)))
        return "OK", [ids]

    def fetch(self, uid: bytes, parts: str) -> tuple[str, list]:
        index = int(uid.decode()) - 1
        if index < 0 or index >= len(self._messages):
            return "NO", []
        raw = self._messages[index].as_bytes()
        # imaplib returns a tuple ((b"uid (..)", b"<raw bytes>"), b")") shape
        return "OK", [(b"%s (BODY[])" % uid, raw)]

    def logout(self) -> None:
        self.logged_out = True


class _IngestTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=ingest.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "listings.sqlite"
        with closing(connect(self.db_path)) as conn:
            init_db(conn)

    def _build_args(self, **overrides) -> argparse.Namespace:
        defaults = dict(
            db=self.db_path,
            mailbox="INBOX",
            days=30,
            limit=50,
            reprocess=False,
            dry_run=False,
            verbose=False,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)


class IngestRoutingTest(_IngestTestBase):
    def test_routes_known_senders_and_dumps_unknown(self) -> None:
        messages = [
            make_email(
                sender="news@homegate.ch",
                subject="Neue Inserate Luzern CHF 900",
                html="<a href='https://www.homegate.ch/rent/4001111'>Wohnung Luzern</a>",
            ),
            make_email(
                sender="random@unknown.example.com",
                subject="not a portal",
                plain="hello",
            ),
        ]
        fake = FakeIMAP(messages)
        with patch.object(ingest, "imap_credentials", return_value=("a@b.com", "pw")), \
             patch.object(ingest, "connect_imap", return_value=fake):
            stats = ingest.sync_email_alerts(self._build_args())

        self.assertEqual(stats.fetched, 2)
        self.assertEqual(stats.routed, 1)
        self.assertEqual(stats.unrouted, 1)
        self.assertEqual(stats.listings_seen, 1)
        self.assertEqual(stats.listings_created, 1)
        self.assertTrue(fake.logged_out)

        # Listing made it into SQLite
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT source, url FROM listings").fetchone()
        self.assertEqual(row[0], "homegate.ch")
        self.assertIn("homegate.ch/rent/4001111", row[1])

    def test_dry_run_does_not_write(self) -> None:
        messages = [
            make_email(
                sender="alerts@flatfox.ch",
                subject="Neue Flatfox Inserate",
                plain="https://flatfox.ch/en/flat/luzern/abc/",
            ),
        ]
        fake = FakeIMAP(messages)
        with patch.object(ingest, "imap_credentials", return_value=("a@b.com", "pw")), \
             patch.object(ingest, "connect_imap", return_value=fake):
            stats = ingest.sync_email_alerts(self._build_args(dry_run=True))

        self.assertEqual(stats.routed, 1)
        self.assertEqual(stats.listings_seen, 1)
        self.assertEqual(stats.listings_created, 0)
        self.assertEqual(stats.listings_skipped, 1)
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            seen_count = conn.execute(
                "SELECT COUNT(*) FROM email_alert_seen"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(seen_count, 0)

    def test_seen_table_dedupes_repeated_runs(self) -> None:
        messages = [
            make_email(
                sender="news@homegate.ch",
                subject="Repeat alert",
                html="<a href='https://www.homegate.ch/rent/4002222'>Wohnung</a>",
                message_id="<repeat@fixture>",
            ),
        ]
        fake = FakeIMAP(messages)
        with patch.object(ingest, "imap_credentials", return_value=("a@b.com", "pw")), \
             patch.object(ingest, "connect_imap", return_value=fake):
            first = ingest.sync_email_alerts(self._build_args())
            second = ingest.sync_email_alerts(self._build_args())

        self.assertEqual(first.listings_created, 1)
        # Second run sees the same message but skips it via email_alert_seen,
        # so listings_seen==0 even though fetched==1.
        self.assertEqual(second.fetched, 1)
        self.assertEqual(second.listings_seen, 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            seen_rows = conn.execute(
                "SELECT message_id, source FROM email_alert_seen"
            ).fetchall()
        self.assertEqual(len(seen_rows), 1)
        self.assertEqual(seen_rows[0][1], "homegate.ch")


class ImapCredentialsTest(unittest.TestCase):
    def test_imap_credentials_raises_when_missing(self) -> None:
        with patch.dict("os.environ", {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}, clear=False):
            with self.assertRaises(SystemExit):
                ingest.imap_credentials()

    def test_imap_credentials_strips_spaces_in_password(self) -> None:
        with patch.dict(
            "os.environ",
            {"GMAIL_ADDRESS": "x@y.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop"},
            clear=False,
        ):
            addr, pwd = ingest.imap_credentials()
        self.assertEqual(addr, "x@y.com")
        self.assertEqual(pwd, "abcdefghijklmnop")


class HelperTest(unittest.TestCase):
    def test_message_identifier_prefers_message_id(self) -> None:
        msg = make_email(
            sender="x@y", subject="z", plain="hi", message_id="<unique@fixture>"
        )
        self.assertEqual(ingest.message_identifier(msg, b"42"), "<unique@fixture>")

    def test_message_identifier_falls_back_to_uid(self) -> None:
        msg = EmailMessage()
        msg["From"] = "x@y"
        msg["Subject"] = "z"
        self.assertEqual(ingest.message_identifier(msg, b"99"), "imap-uid:99")


if __name__ == "__main__":
    unittest.main()
