"""
Shared per-stage helpers: loud-fail on 0/N and unconditional run-summary writer.

Extracted from generate_cover_letter.py after the 2026-05-08 font-bug
post-mortem. The CL stage silently produced 0/49 cover letters because every
job hit FPDFUnicodeEncodingException inside a per-job try/except, the loop
exited 0, and no summary file was written — so post-mortem required Volume
archaeology. Same pattern existed in generate_cv.py, send_followups.py, and
update_cover_letter_dates.py at varying severity.

These two helpers make the CL fix portable so every long per-job loop in
the pipeline gets the same detection + post-mortem instrumentation without
duplicating the logic by hand.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def require_nonzero_success(
    successful: int,
    pending: int,
    stage: str,
    summary_path: Path | None = None,
    threshold: int = 3,
) -> None:
    """Exit 1 if 0 succeeded out of >= threshold pending. Caller-side post-mortem.

    Threshold guards against tiny-batch noise: a single hallucination giveup
    on a 1-job day shouldn't page anyone. ≥3 pending is the floor we used in
    generate_cover_letter.py.
    """
    if successful == 0 and pending >= threshold:
        suffix = f" See {summary_path.name} on the volume." if summary_path else ""
        log.error(
            f"SYSTEMIC FAILURE [{stage}]: 0/{pending} items succeeded.{suffix} "
            f"Exiting non-zero so the pipeline alerts."
        )
        sys.exit(1)


def write_run_summary(
    path: Path,
    *,
    stage: str,
    successful: int,
    failed: int,
    errors: list[dict[str, Any]],
    **extra: Any,
) -> None:
    """Always-write JSON summary for post-mortem.

    Unlike quality_report.json (gated on successful>0 in CL), this is written
    even when every job failed — that's exactly when you most need it.
    `errors` should already contain dicts with type-prefixed error strings
    (e.g. {"job_id": ..., "error": "FPDFUnicodeEncodingException: ..."}).
    """
    payload = {
        "stage": stage,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "successful_count": successful,
        "failed_count": failed,
        "errors": errors,
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Run summary saved: {path} ({successful} ok / {failed} failed)")
