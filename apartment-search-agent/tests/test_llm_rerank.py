import io
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from execution import llm_rerank as lr
from execution.apartment_pipeline import (
    ListingInput,
    connect,
    init_db,
    normalize_listing,
    score_listing,
    upsert_listing,
)


def _seed_consider(conn, *, city="Luzern", rent=850, n=1) -> list[int]:
    """Insert n listings and force them into the 'consider' tier (the re-rank's
    target), returning their ids."""
    ids: list[int] = []
    for index in range(n):
        listing = ListingInput(
            url=f"https://flatfox.ch/listing/consider-{city}-{index}",
            source="flatfox",
            title=f"WG Zimmer {city} {index}",
            rent_chf=rent,
            city=city,
            move_in="16.07.",
            contact_name=None,
            contact_email=None,
            raw_text="ruhig sauber WG Anmeldung moeglich",
            commute_minutes=None,
        )
        listing_id, _ = upsert_listing(conn, score_listing(normalize_listing(listing)))
        conn.execute("UPDATE listings SET decision = 'consider' WHERE id = ?", (listing_id,))
        ids.append(listing_id)
    conn.commit()
    return ids


def _fake_openrouter(score: int, reason: str) -> dict:
    import json

    return {"choices": [{"message": {"content": json.dumps({"score": score, "reason": reason})}}]}


class PromptTest(unittest.TestCase):
    def test_prompt_includes_facts_and_requests_json(self) -> None:
        row = {
            "city": "Buchrain",
            "rent_chf": 720,
            "commute_class": "A",
            "commute_minutes": 22,
            "wg_fit_score": 70,
            "flags_json": '["furnished"]',
            "move_in": "16.07.",
            "raw_text": "ruhig sauber WG Anmeldung",
        }
        prompt = lr.build_prompt(row)
        self.assertIn("Buchrain", prompt)
        self.assertIn("CHF 720", prompt)
        self.assertIn("A / 22 min", prompt)
        self.assertIn("furnished", prompt)
        self.assertIn("Root D4", prompt)          # criteria present
        self.assertIn("STRICT JSON", prompt)       # output contract present
        self.assertIn('"score"', prompt)

    def test_prompt_truncates_long_raw_text(self) -> None:
        row = {"city": "Luzern", "raw_text": "x" * 5000}
        prompt = lr.build_prompt(row)
        self.assertIn("...", prompt)
        self.assertLess(len(prompt), 5000)


class ParseTest(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        self.assertEqual(lr.parse_score_json('{"score": 82, "reason": "great fit"}'), (82, "great fit"))

    def test_parses_fenced_json(self) -> None:
        self.assertEqual(
            lr.parse_score_json('```json\n{"score": 50, "reason": "ok"}\n```'),
            (50, "ok"),
        )

    def test_extracts_json_amid_prose(self) -> None:
        self.assertEqual(
            lr.parse_score_json('Sure! {"score": 33, "reason": "meh"} hope that helps'),
            (33, "meh"),
        )

    def test_clamps_out_of_range_score(self) -> None:
        self.assertEqual(lr.parse_score_json('{"score": 130, "reason": "x"}')[0], 100)
        self.assertEqual(lr.parse_score_json('{"score": -5, "reason": "x"}')[0], 0)

    def test_truncates_long_reason(self) -> None:
        score, reason = lr.parse_score_json('{"score": 60, "reason": "' + "a" * 300 + '"}')
        self.assertEqual(score, 60)
        self.assertEqual(len(reason), 140)

    def test_rejects_malformed_and_empty(self) -> None:
        self.assertIsNone(lr.parse_score_json("not json at all"))
        self.assertIsNone(lr.parse_score_json(""))
        self.assertIsNone(lr.parse_score_json(None))
        self.assertIsNone(lr.parse_score_json('{"reason": "no score key"}'))
        self.assertIsNone(lr.parse_score_json('{"score": "high"}'))


class ModelResolutionTest(unittest.TestCase):
    def test_default_model(self) -> None:
        with patch.dict("os.environ", {"MODEL_RERANK": "", "OPENROUTER_MODEL": ""}, clear=False):
            self.assertEqual(lr.resolve_model(), "google/gemma-4-31b")

    def test_env_override_precedence(self) -> None:
        with patch.dict("os.environ", {"MODEL_RERANK": "x/custom", "OPENROUTER_MODEL": "y/other"}):
            self.assertEqual(lr.resolve_model(), "x/custom")
        with patch.dict("os.environ", {"MODEL_RERANK": "", "OPENROUTER_MODEL": "y/other"}):
            self.assertEqual(lr.resolve_model(), "y/other")


class RunTest(unittest.TestCase):
    def _db(self, tmpdir: str) -> Path:
        return Path(tmpdir) / "listings.sqlite"

    def _decision(self, db_path: Path, listing_id: int) -> str:
        with closing(connect(db_path)) as conn:
            return conn.execute(
                "SELECT decision FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()[0]

    def _scores(self, db_path: Path, listing_id: int):
        with closing(connect(db_path)) as conn:
            row = conn.execute(
                "SELECT openrouter_score, openrouter_reason FROM listings WHERE id = ?",
                (listing_id,),
            ).fetchone()
        return row[0], row[1]

    def test_dry_run_makes_zero_calls_and_prints_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)
                _seed_consider(conn, n=2)

            buf = io.StringIO()
            with patch.object(lr, "_post_json") as post, redirect_stdout(buf):
                rc = lr.main(["--db", str(db_path)])  # dry-run is the default

            self.assertEqual(rc, 0)
            post.assert_not_called()
            out = buf.getvalue()
            self.assertIn("DRY-RUN", out)
            self.assertIn("Candidates", out)
            self.assertIn("Root D4", out)  # the rendered prompt was printed

    def test_send_writes_scores_and_leaves_decision_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing_id = _seed_consider(conn, n=1)[0]

            with patch.object(lr, "api_key", return_value="fake-key"), \
                 patch.object(lr, "_post_json", return_value=_fake_openrouter(82, "calm WG, good price")):
                rc = lr.main(["--db", str(db_path), "--send"])

            self.assertEqual(rc, 0)
            score, reason = self._scores(db_path, listing_id)
            self.assertEqual(score, 82)
            self.assertEqual(reason, "calm WG, good price")
            # Advisory only — the deterministic decision is untouched.
            self.assertEqual(self._decision(db_path, listing_id), "consider")

    def test_send_skips_malformed_response_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing_id = _seed_consider(conn, n=1)[0]

            bad_body = {"choices": [{"message": {"content": "sorry, I can't do that"}}]}
            with patch.object(lr, "api_key", return_value="fake-key"), \
                 patch.object(lr, "_post_json", return_value=bad_body):
                rc = lr.main(["--db", str(db_path), "--send"])

            self.assertEqual(rc, 0)
            score, reason = self._scores(db_path, listing_id)
            self.assertIsNone(score)
            self.assertIsNone(reason)
            self.assertEqual(self._decision(db_path, listing_id), "consider")

    def test_no_api_key_is_logged_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)
                listing_id = _seed_consider(conn, n=1)[0]

            buf = io.StringIO()
            with patch.object(lr, "api_key", return_value=None), \
                 patch.object(lr, "_post_json") as post, redirect_stdout(buf):
                rc = lr.main(["--db", str(db_path), "--send"])

            self.assertEqual(rc, 0)
            post.assert_not_called()
            self.assertIn("no-op", buf.getvalue())
            self.assertIsNone(self._scores(db_path, listing_id)[0])

    def test_max_rows_caps_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)
                _seed_consider(conn, n=3)

            with patch.object(lr, "api_key", return_value="fake-key"), \
                 patch.object(lr, "_post_json", return_value=_fake_openrouter(70, "ok")) as post:
                rc = lr.main(["--db", str(db_path), "--send", "--max-rows", "2"])

            self.assertEqual(rc, 0)
            self.assertEqual(post.call_count, 2)

    def test_no_candidates_is_clean_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._db(tmpdir)
            with closing(connect(db_path)) as conn:
                init_db(conn)  # empty table → no 'consider' rows
            with patch.object(lr, "_post_json") as post:
                rc = lr.main(["--db", str(db_path), "--send"])
            self.assertEqual(rc, 0)
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
