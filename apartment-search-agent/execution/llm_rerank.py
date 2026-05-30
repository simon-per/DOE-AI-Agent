#!/usr/bin/env python3
"""Advisory LLM re-rank of the ``consider`` tier (manual, opt-in, capped).

A cheap second-pass qualitative score via OpenRouter. It is **advisory only**:
it writes nothing but ``openrouter_score`` / ``openrouter_reason`` and NEVER
changes the deterministic ``decision``, ``priority_score``, or any other field.
The deterministic pipeline remains the source of truth — this only re-orders /
annotates within the ``consider`` tier for display, the brief, and the Sheet.

Safety:
- DRY-RUN by default. Prints the resolved model, the rendered prompt for the
  first candidate, and the candidate count — and makes ZERO API calls.
- ``--send`` performs the calls and writes the two columns. ``--max-rows`` caps
  spend (~one short call per listing).
- No ``OPENROUTER_API_KEY`` → logged no-op (still zero calls).
- Malformed / empty model output is skipped (no crash, no write).

Stdlib only — no new dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from execution.apartment_pipeline import (  # noqa: E402
    DEFAULT_DB_PATH,
    connect,
    init_db,
    now_iso,
    row_to_dict,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Best quality-for-price in Simon's leaderboard (rank ~6, sub-cent per run).
# Override per-run with MODEL_RERANK or OPENROUTER_MODEL.
DEFAULT_MODEL = "google/gemma-4-31b"
DEFAULT_MAX_ROWS = 40
DEFAULT_TIMEOUT = 30
RAW_TEXT_EXCERPT_CHARS = 600
USER_AGENT = "apartment-search-agent/0.1 (+advisory re-rank; low volume)"

# Simon's fixed criteria, distilled from AGENTS.md / ABOUTME.md. Kept here so the
# prompt is deterministic and reviewable (not silently drifting per run).
CRITERIA = (
    "Simon is a 23-year-old man starting at PHENOGY in Root D4 on 2026-07-16, "
    "commuting by public transport and e-bike. He wants a calm, clean, reliable "
    "WG (shared flat) or small apartment. Rank HIGHER: short door-to-door commute "
    "to Root D4 (<=20 min is excellent, <=30 min strong), rent <= CHF 1000 "
    "(ideal < CHF 800), Anmeldung/registration possible, a WG that is social but "
    "not a party flat, stable / longer-term. Rank LOWER: long commute, over "
    "budget, party vibe, very short-term sublet, or anything that reads like a "
    "scam. Listings restricted to women only are disqualifying (score 0)."
)

PROMPT_INSTRUCTIONS = (
    "Score how well this listing fits Simon from 0 to 100 (100 = perfect fit). "
    "Reply with STRICT JSON and nothing else: "
    '{"score": <integer 0-100>, "reason": "<reason, <=140 chars>"}.'
)


def _load_env_chain() -> None:
    """Load this project's .env then the parent's (override=False), mirroring
    commute_scoring so a shared parent key is picked up without clobbering a
    project-local override."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")


def api_key() -> str | None:
    _load_env_chain()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key or None


def resolve_model() -> str:
    for var in ("MODEL_RERANK", "OPENROUTER_MODEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return DEFAULT_MODEL


def select_candidates(conn, limit: int) -> list[dict[str, Any]]:
    """The ``consider`` tier, most-promising first, capped at ``limit``."""
    init_db(conn)
    rows = conn.execute(
        """
        SELECT * FROM listings
        WHERE decision = 'consider'
          AND status NOT IN ('sent', 'archived', 'expired')
        ORDER BY priority_score DESC, id ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def _flags_text(row: dict[str, Any]) -> str:
    try:
        flags = json.loads(row.get("flags_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        flags = []
    return ", ".join(str(f) for f in flags) if flags else "none"


def build_prompt(row: dict[str, Any]) -> str:
    rent = row.get("rent_chf")
    rent_text = f"CHF {rent}" if rent is not None else "unknown"
    commute_min = row.get("commute_minutes")
    klass = row.get("commute_class") or "?"
    commute = f"{klass} / {commute_min} min" if commute_min is not None else klass
    wg = row.get("wg_fit_score")
    excerpt = " ".join((row.get("raw_text") or "").split())
    if len(excerpt) > RAW_TEXT_EXCERPT_CHARS:
        excerpt = excerpt[:RAW_TEXT_EXCERPT_CHARS] + "..."
    facts = "\n".join(
        [
            f"City: {row.get('city') or '?'}",
            f"Rent: {rent_text}",
            f"Commute to Root D4: {commute}",
            f"WG-fit heuristic (0-100): {wg if wg is not None else '?'}",
            f"Flags: {_flags_text(row)}",
            f"Move-in: {row.get('move_in') or '?'}",
            f"Listing text: {excerpt or '(none)'}",
        ]
    )
    return f"{CRITERIA}\n\nLISTING:\n{facts}\n\n{PROMPT_INSTRUCTIONS}"


def _post_json(url: str, payload: dict, headers: dict, *, timeout: int = DEFAULT_TIMEOUT) -> dict | None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_score_json(content: str | None) -> tuple[int, str] | None:
    """Parse a model reply into (score, reason). Tolerates ```json fences and a
    little surrounding prose; returns None on anything unusable."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    try:
        score = int(round(float(obj["score"])))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))
    reason = str(obj.get("reason") or "").strip()[:140]
    return score, reason


def parse_response(body: dict | None) -> tuple[int, str] | None:
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return parse_score_json(content)


def score_one(prompt: str, *, model: str, key: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str] | None:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        # OpenRouter attribution headers (optional, harmless).
        "X-Title": "apartment-search-agent re-rank",
    }
    try:
        body = _post_json(OPENROUTER_URL, payload, headers, timeout=timeout)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"[llm_rerank] API call failed: {exc!r}", file=sys.stderr)
        return None
    return parse_response(body)


def write_score(conn, listing_id: int, score: int, reason: str) -> None:
    # Writes ONLY the advisory columns — decision and all deterministic fields
    # (and updated_at, to avoid perturbing the notifier's new-listing window)
    # are left untouched.
    conn.execute(
        "UPDATE listings SET openrouter_score = ?, openrouter_reason = ? WHERE id = ?",
        (score, reason, listing_id),
    )
    conn.commit()


def run(args: argparse.Namespace) -> int:
    _load_env_chain()
    model = resolve_model()
    with closing(connect(args.db)) as conn:
        candidates = select_candidates(conn, args.max_rows)
        print(f"Re-rank model: {model}")
        print(
            f"Candidates (decision=consider, capped at --max-rows={args.max_rows}): "
            f"{len(candidates)}"
        )
        if not candidates:
            print("Nothing to score.")
            return 0

        if args.dry_run:
            print("\nDRY-RUN — no API calls. Rendered prompt for the first candidate:\n")
            print("-" * 72)
            print(build_prompt(candidates[0]))
            print("-" * 72)
            print(f"\n{len(candidates)} candidate(s) would be scored with --send.")
            return 0

        key = api_key()
        if not key:
            print("OPENROUTER_API_KEY not set — re-rank is a no-op (no calls made).")
            return 0

        scored = 0
        skipped = 0
        for row in candidates:
            result = score_one(build_prompt(row), model=model, key=key)
            if result is None:
                skipped += 1
                print(f"  [skip] id={row['id']} {row.get('city') or '?'} — no usable score")
                continue
            score, reason = result
            write_score(conn, row["id"], score, reason)
            scored += 1
            print(f"  [ok]   id={row['id']} {row.get('city') or '?'} -> {score} — {reason}")
        print(f"\nScored {scored}, skipped {skipped}. Deterministic decision unchanged for all.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Advisory LLM re-rank of the 'consider' tier via OpenRouter. "
            "Dry-run by default; advisory only — never changes the deterministic decision."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"Cap on candidates scored per run (cost cap, default {DEFAULT_MAX_ROWS}).",
    )
    parser.add_argument(
        "--send",
        dest="dry_run",
        action="store_false",
        help="Perform the API calls and write scores. Default is dry-run (zero calls).",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the prompt + candidate count and make zero API calls (default).",
    )
    parser.set_defaults(dry_run=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_rows < 1:
        raise SystemExit("--max-rows must be at least 1.")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
