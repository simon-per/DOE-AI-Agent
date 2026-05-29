import argparse
import sqlite3
import tempfile
import unittest
from contextlib import closing
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from execution import manual_paste
from execution.apartment_pipeline import connect, init_db


SAMPLE_BATCH = """\
url: https://www.facebook.com/groups/12345/posts/67890
source: facebook
contact_email: anna@example.ch

WG-Zimmer in Buchrain, CHF 720, ab 16.07.
Ruhig, sauber, Anmeldung möglich. Bei Interesse bitte melden.
===
url: https://www.anibis.ch/de/d/wohnung-mieten-luzern-99887
rent: 950

Schöne 2-Zimmer Wohnung in Luzern, frei ab 1. August.
===
this block has no url header so it should be skipped
"""


class SplitBlocksTest(unittest.TestCase):
    def test_blocks_split_on_triple_equals(self) -> None:
        blocks = manual_paste.split_blocks(SAMPLE_BATCH)
        self.assertEqual(len(blocks), 3)
        self.assertIn("Buchrain", blocks[0])
        self.assertIn("Luzern", blocks[1])
        self.assertIn("no url header", blocks[2])

    def test_empty_input_yields_no_blocks(self) -> None:
        self.assertEqual(manual_paste.split_blocks(""), [])
        self.assertEqual(manual_paste.split_blocks("\n\n===\n\n"), [])


class ParseBlockTest(unittest.TestCase):
    def test_parses_headers_and_body(self) -> None:
        block = (
            "url: https://example.test/x\n"
            "source: facebook\n"
            "contact_email: foo@bar.tld\n"
            "rent: CHF 750\n"
            "\n"
            "Body text mentioning Luzern and CHF 750."
        )
        listing = manual_paste.parse_block(block)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.url, "https://example.test/x")
        self.assertEqual(listing.source, "facebook")
        self.assertEqual(listing.contact_email, "foo@bar.tld")
        self.assertEqual(listing.rent_chf, 750)
        self.assertIn("Body text", listing.raw_text)

    def test_no_url_returns_none(self) -> None:
        block = "source: anibis\n\nNo URL here, can't dedupe."
        self.assertIsNone(manual_paste.parse_block(block))

    def test_body_only_block_with_url_in_text_returns_none(self) -> None:
        # URL must appear as a header — body-only blocks can't be tracked.
        block = "https://example.test/x just floats in the body"
        self.assertIsNone(manual_paste.parse_block(block))

    def test_unknown_header_stops_header_parsing(self) -> None:
        block = (
            "url: https://example.test/y\n"
            "weird_header: ignored\n"
            "\n"
            "body."
        )
        listing = manual_paste.parse_block(block)
        self.assertIsNotNone(listing)
        # weird_header line lands in the body, not headers
        self.assertIn("weird_header", listing.raw_text)


class IngestBlocksTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(dir=manual_paste.BASE_DIR / ".tmp")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "listings.sqlite"
        with closing(connect(self.db_path)) as conn:
            init_db(conn)

    def _args(self, **overrides) -> argparse.Namespace:
        defaults = dict(db=self.db_path, dry_run=False, verbose=False)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_ingest_creates_rows_and_skips_no_url(self) -> None:
        blocks = manual_paste.split_blocks(SAMPLE_BATCH)
        stats = manual_paste.ingest_blocks(blocks, self._args())
        self.assertEqual(stats.blocks_seen, 3)
        self.assertEqual(stats.blocks_skipped, 1)
        self.assertEqual(stats.created, 2)
        self.assertEqual(stats.updated, 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            urls = {row[0] for row in conn.execute("SELECT url FROM listings").fetchall()}
        self.assertEqual(len(urls), 2)

    def test_dry_run_writes_nothing(self) -> None:
        blocks = manual_paste.split_blocks(SAMPLE_BATCH)
        stats = manual_paste.ingest_blocks(blocks, self._args(dry_run=True))
        self.assertEqual(stats.created, 0)
        self.assertEqual(stats.updated, 0)
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        self.assertEqual(count, 0)

    def test_repeated_ingest_updates_existing(self) -> None:
        blocks = manual_paste.split_blocks(SAMPLE_BATCH)
        manual_paste.ingest_blocks(blocks, self._args())
        stats = manual_paste.ingest_blocks(blocks, self._args())
        self.assertEqual(stats.created, 0)
        self.assertEqual(stats.updated, 2)


class CliTest(unittest.TestCase):
    def test_main_reads_from_file(self) -> None:
        tmp_root = manual_paste.BASE_DIR / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmpdir:
            tmp = Path(tmpdir)
            batch_file = tmp / "batch.txt"
            batch_file.write_text(SAMPLE_BATCH, encoding="utf-8")
            db_path = tmp / "listings.sqlite"
            with closing(connect(db_path)) as conn:
                init_db(conn)
            rc = manual_paste.main(["--db", str(db_path), "--file", str(batch_file)])
        self.assertEqual(rc, 0)

    def test_main_requires_some_input(self) -> None:
        # No --file, stdin is a TTY → should SystemExit
        with patch.object(manual_paste.sys, "stdin", StringIO("")) as fake_stdin:
            fake_stdin.isatty = lambda: True
            with self.assertRaises(SystemExit):
                manual_paste.main(["--db", str(self._tmp_db())])

    def _tmp_db(self) -> Path:
        tmp_root = manual_paste.BASE_DIR / ".tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        tmpdir = tempfile.mkdtemp(dir=tmp_root)
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, ignore_errors=True))
        return Path(tmpdir) / "listings.sqlite"


if __name__ == "__main__":
    unittest.main()
