"""Weekly digest: pipeline metrics emailed every Sunday evening.

Reads the Google Sheet, slices the data along the dimensions that actually matter
for tuning the system (score band, contact source, follow-up touch count, response
type), and emails a plaintext summary to the candidate.

The digest is read-only on the sheet — no rows are modified. Failures are silent
on the data side and best-effort on the email side, so a flaky sheet read or a
Gmail outage cannot affect any other pipeline stage.

Usage:
    python execution/weekly_digest.py             # send the email
    python execution/weekly_digest.py --dry-run   # print the body, don't send
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("weekly_digest")

SHEET_NAME = "Swiss Job Search Pipeline"
ALERT_EMAIL = "simonobemair@gmail.com"

# Reply rate is computed only over rows old enough to *plausibly* have replied.
# 14 business days ≈ 3 calendar weeks — a reasonable maturity window.
REPLY_RATE_MATURITY_BD = 14


# ---------------------------------------------------------------------------
# Date helpers (Mon–Sun week, business-day arithmetic)
# ---------------------------------------------------------------------------

def _business_days_between(start: date, end: date) -> int:
    """Count business days (Mon–Fri), exclusive of start, inclusive of end. Mirror of send_followups."""
    if end <= start:
        return 0
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def _parse_sheet_date(s: str) -> date | None:
    """Try the formats this codebase writes (DD.MM.YYYY first, then ISO + slash)."""
    if not s or not s.strip():
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _week_bounds(today: date) -> tuple[date, date]:
    """Monday–Sunday window containing `today`."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _row_value(row: list[str], header_to_idx: dict[str, int], key: str) -> str:
    """Safe, trimmed cell access by header name."""
    idx = header_to_idx.get(key)
    if idx is None:
        return ""
    return row[idx].strip() if idx < len(row) else ""


def _score_band(score_str: str) -> str:
    """Bucket a numeric score into 5–6 / 7–8 / 9–10 / other."""
    try:
        s = int(score_str)
    except (ValueError, TypeError):
        return "—"
    if 9 <= s <= 10:
        return "9–10"
    if 7 <= s <= 8:
        return "7–8"
    if 5 <= s <= 6:
        return "5–6"
    if 1 <= s <= 4:
        return "1–4"
    return "—"


def _compute_metrics(rows: list[list[str]], header: list[str], today: date) -> dict:
    """Single pass over the data rows producing every metric the digest displays."""
    h2i = {h.strip(): i for i, h in enumerate(header)}

    monday, sunday = _week_bounds(today)
    metrics: dict = {
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "total_rows": 0,
        "applied_this_week": 0,
        "active_pipeline": 0,           # Status in {Applied, Follow-Up_Sent}
        "applied_total": 0,
        "responses_total": 0,
        "applied_mature": 0,            # rows ≥ REPLY_RATE_MATURITY_BD bd old
        "responses_mature": 0,          # responses on those mature rows
        "by_score_band": {},            # band → {"applied": n, "responses": n}
        "by_contact_source": {},        # source → {"applied": n, "responses": n}
        "by_has_contact_name": {        # bool key → {"applied": n, "responses": n}
            "with_name": {"applied": 0, "responses": 0},
            "without_name": {"applied": 0, "responses": 0},
        },
        "touch_distribution": Counter(),  # 0/1/2/3
        "response_types": Counter(),
        "top_quality_recent": [],         # top CL_Quality_Score rows applied this week
        "avg_quality_week": None,
        "avg_quality_prior_4w": None,
    }

    quality_week: list[float] = []
    quality_prior_4w: list[float] = []
    week_minus_4 = monday - timedelta(weeks=4)
    top_recent: list[tuple[float, str, str]] = []  # (score, company, title)

    for row in rows:
        metrics["total_rows"] += 1

        status = _row_value(row, h2i, "Status").lower()
        date_applied_str = _row_value(row, h2i, "Date_Applied")
        date_applied = _parse_sheet_date(date_applied_str)

        # Active pipeline: anything Applied or in follow-up
        if status in ("applied", "follow-up_sent"):
            metrics["active_pipeline"] += 1

        # Applied-this-week metric
        if date_applied and monday <= date_applied <= sunday:
            metrics["applied_this_week"] += 1

        # Skip the response-rate accounting for non-applied rows
        if status not in ("applied", "follow-up_sent", "interviewing", "offer", "rejected", "no_response"):
            continue
        if not date_applied:
            continue

        metrics["applied_total"] += 1

        # Outcome signal: prefer explicit Response_Received, fall back to status
        rr = _row_value(row, h2i, "Response_Received").lower()
        rtype = _row_value(row, h2i, "Response_Type")
        responded = rr == "yes" or status in ("interviewing", "offer", "rejected")
        if responded:
            metrics["responses_total"] += 1
            metrics["response_types"][rtype or status.title() or "Unknown"] += 1

        # Maturity-windowed reply rate (only count rows old enough to plausibly have replied)
        bd = _business_days_between(date_applied, today)
        if bd >= REPLY_RATE_MATURITY_BD:
            metrics["applied_mature"] += 1
            if responded:
                metrics["responses_mature"] += 1

        # Score band split
        band = _score_band(_row_value(row, h2i, "Score"))
        bucket = metrics["by_score_band"].setdefault(band, {"applied": 0, "responses": 0})
        bucket["applied"] += 1
        if responded:
            bucket["responses"] += 1

        # Contact source split (Phase 2 column — empty for legacy rows)
        cs = _row_value(row, h2i, "Contact_Source") or "Manual/Unknown"
        cs_bucket = metrics["by_contact_source"].setdefault(cs, {"applied": 0, "responses": 0})
        cs_bucket["applied"] += 1
        if responded:
            cs_bucket["responses"] += 1

        # Has-contact-name split
        cp = _row_value(row, h2i, "Contact_Person")
        key = "with_name" if cp else "without_name"
        metrics["by_has_contact_name"][key]["applied"] += 1
        if responded:
            metrics["by_has_contact_name"][key]["responses"] += 1

        # Touch distribution (count from Follow_Up_Count, fall back to Follow_Up_Sent legacy)
        fc_raw = _row_value(row, h2i, "Follow_Up_Count")
        if fc_raw:
            try:
                fc = max(0, min(3, int(fc_raw)))
            except (ValueError, TypeError):
                fc = 1 if _row_value(row, h2i, "Follow_Up_Sent").lower() == "yes" else 0
        else:
            fc = 1 if _row_value(row, h2i, "Follow_Up_Sent").lower() == "yes" else 0
        metrics["touch_distribution"][fc] += 1

        # CL quality scores
        cl_q = _row_value(row, h2i, "CL_Quality_Score")
        score_val = _parse_quality_score(cl_q)
        if score_val is not None:
            if monday <= date_applied <= sunday:
                quality_week.append(score_val)
                top_recent.append((score_val, _row_value(row, h2i, "Company"),
                                   _row_value(row, h2i, "Title")))
            elif week_minus_4 <= date_applied < monday:
                quality_prior_4w.append(score_val)

    if quality_week:
        metrics["avg_quality_week"] = sum(quality_week) / len(quality_week)
    if quality_prior_4w:
        metrics["avg_quality_prior_4w"] = sum(quality_prior_4w) / len(quality_prior_4w)

    top_recent.sort(reverse=True)
    metrics["top_quality_recent"] = top_recent[:5]

    return metrics


def _parse_quality_score(s: str) -> float | None:
    """Quality column is written as 'X/5' (or sometimes a bare number). Both are accepted."""
    if not s:
        return None
    s = s.strip()
    if "/" in s:
        s = s.split("/", 1)[0].strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "—"
    return f"{(100 * numerator / denominator):.1f}%"


def render_digest(metrics: dict) -> tuple[str, str]:
    """Build (subject, body) for the digest email. Pure formatting, no side effects."""
    subj = (
        f"[DOE digest] Week of {metrics['week_start']}–{metrics['week_end']}: "
        f"{metrics['applied_this_week']} apps, "
        f"{_pct(metrics['responses_mature'], metrics['applied_mature'])} reply rate"
    )

    lines: list[str] = []
    lines.append(f"Weekly digest — week of {metrics['week_start']} to {metrics['week_end']}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"This week:           {metrics['applied_this_week']} application(s) sent")
    lines.append(f"Active pipeline:     {metrics['active_pipeline']} row(s) (Applied + Follow-Up_Sent)")
    lines.append(f"Cumulative applied:  {metrics['applied_total']}")
    lines.append("")

    lines.append("REPLY RATE")
    lines.append("-" * 72)
    lines.append(
        f"Mature window (≥ {REPLY_RATE_MATURITY_BD} bd elapsed):  "
        f"{metrics['responses_mature']}/{metrics['applied_mature']} "
        f"({_pct(metrics['responses_mature'], metrics['applied_mature'])})"
    )
    lines.append(
        f"All-time:  "
        f"{metrics['responses_total']}/{metrics['applied_total']} "
        f"({_pct(metrics['responses_total'], metrics['applied_total'])})"
    )
    lines.append("")

    lines.append("BY SCORE BAND")
    lines.append("-" * 72)
    for band in ("9–10", "7–8", "5–6", "1–4", "—"):
        b = metrics["by_score_band"].get(band)
        if not b or b["applied"] == 0:
            continue
        lines.append(f"  {band:<8}  {b['responses']}/{b['applied']} "
                     f"({_pct(b['responses'], b['applied'])})")
    lines.append("")

    lines.append("BY CONTACT SOURCE (Phase 2 — Stage 4.5)")
    lines.append("-" * 72)
    sources_sorted = sorted(metrics["by_contact_source"].items(),
                            key=lambda kv: -kv[1]["applied"])
    for src, b in sources_sorted:
        lines.append(f"  {src:<18}  {b['responses']}/{b['applied']} "
                     f"({_pct(b['responses'], b['applied'])})")
    lines.append("")

    lines.append("HAS CONTACT NAME (personalized salutation)")
    lines.append("-" * 72)
    wn = metrics["by_has_contact_name"]["with_name"]
    won = metrics["by_has_contact_name"]["without_name"]
    lines.append(f"  with name     {wn['responses']}/{wn['applied']} "
                 f"({_pct(wn['responses'], wn['applied'])})")
    lines.append(f"  without name  {won['responses']}/{won['applied']} "
                 f"({_pct(won['responses'], won['applied'])})")
    lines.append("")

    lines.append("FOLLOW-UP TOUCH DISTRIBUTION (active pipeline)")
    lines.append("-" * 72)
    td = metrics["touch_distribution"]
    for k in (0, 1, 2, 3):
        lines.append(f"  touch {k}: {td.get(k, 0)} row(s)")
    lines.append("")

    if metrics["response_types"]:
        lines.append("RESPONSE TYPES")
        lines.append("-" * 72)
        for rt, n in metrics["response_types"].most_common():
            lines.append(f"  {rt:<14}  {n}")
        lines.append("")

    if metrics["top_quality_recent"]:
        lines.append("TOP CL QUALITY SCORES THIS WEEK")
        lines.append("-" * 72)
        for s, comp, title in metrics["top_quality_recent"]:
            t = (title[:50] + "…") if len(title) > 50 else title
            lines.append(f"  {s:.1f}/5   {comp:<24}  {t}")
        lines.append("")

    if metrics["avg_quality_week"] is not None:
        prior = metrics.get("avg_quality_prior_4w")
        prior_str = f"prior 4w avg: {prior:.2f}" if prior is not None else "no prior data"
        lines.append(f"Avg CL quality (week): {metrics['avg_quality_week']:.2f}/5  "
                     f"({prior_str})")
        lines.append("")

    lines.append("-" * 72)
    lines.append("Manually fill Response_Received / Response_Type / Outcome_Notes "
                 "in the sheet to feed future digests. (Auto-detection is Phase 3.)")
    return subj, "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Email weekly pipeline digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest body and exit without sending")
    args = parser.parse_args()

    log.info("Reading Google Sheet…")
    try:
        from execution.write_jobs_to_sheet import authenticate, _sheets_api_call
        client = authenticate()
        ws = _sheets_api_call(client.open, SHEET_NAME).sheet1
        all_rows = _sheets_api_call(ws.get_all_values)
    except Exception as exc:  # noqa: BLE001 — never abort the parent pipeline
        log.error(f"Sheet read failed: {type(exc).__name__}: {exc}")
        return 0

    if not all_rows or len(all_rows) < 2:
        log.info("Sheet empty; nothing to summarise")
        return 0

    header = [h.strip() for h in all_rows[0]]
    today = datetime.now().date()
    metrics = _compute_metrics(all_rows[1:], header, today)

    subject, body = render_digest(metrics)

    log.info(f"Subject: {subject}")
    log.info(f"Body length: {len(body)} chars")

    if args.dry_run:
        print("\n" + body)
        return 0

    try:
        from execution.gmail_send import send_email
        ok = send_email(subject=subject, body=body, to=ALERT_EMAIL)
        if ok:
            log.info(f"Digest emailed to {ALERT_EMAIL}")
        else:
            log.warning("Digest send returned False (gmail_send swallowed an error)")
    except Exception as exc:  # noqa: BLE001
        log.error(f"Digest send failed: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
