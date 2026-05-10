"""
Delete application folders whose Job_ID is no longer present in the Sheet.

Google Sheet is the source of truth. This tool compares Sheet Job_ID values to
local `.tmp/applications/J-*` folders and active Drive `DOE Applications/J-*`
folders. It is dry-run by default and requires both `--apply` and `--yes` before
any local or Drive deletion happens.

Examples:
    python -m execution.cleanup_orphan_application_folders
    python -m execution.cleanup_orphan_application_folders --targets local
    python -m execution.cleanup_orphan_application_folders --apply --yes --targets local
    python -m execution.cleanup_orphan_application_folders --apply --yes --targets both --force
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution._stage_helpers import write_run_summary  # noqa: E402
from execution.write_jobs_to_sheet import authenticate, _sheets_api_call  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cleanup_orphan_application_folders")

DEFAULT_SHEET_NAME = "Swiss Job Search Pipeline"
DEFAULT_LOCAL_ROOT = PROJECT_ROOT / ".tmp" / "applications"
DEFAULT_SUMMARY = PROJECT_ROOT / ".tmp" / "orphan_cleanup_summary.json"
JOB_ID_RE = re.compile(r"^J-[A-Za-z0-9]+$")


@dataclass(frozen=True)
class LocalFolder:
    path: Path
    job_id: str
    modified_at: datetime | None


@dataclass(frozen=True)
class DriveFolder:
    raw: dict[str, Any]
    job_id: str
    modified_at: datetime | None


def _normalize_header(header: str) -> str:
    return header.strip().lower().replace(" ", "_")


def _folder_job_id(name: str) -> str | None:
    job_id = name.split("_", 1)[0].strip()
    if JOB_ID_RE.match(job_id):
        return job_id
    return None


def _parse_drive_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_modified_at(folder: Path) -> datetime | None:
    """Return newest mtime for the folder and direct child files.

    Application folders are flat today. Looking at direct children avoids
    relying only on directory mtime, which can be platform-dependent.
    """
    mtimes: list[float] = []
    try:
        mtimes.append(folder.stat().st_mtime)
        for child in folder.iterdir():
            try:
                mtimes.append(child.stat().st_mtime)
            except OSError:
                log.warning(f"[local] cannot stat child, treating folder as protected: {child}")
                return None
    except OSError:
        log.warning(f"[local] cannot stat folder, treating as protected: {folder}")
        return None
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=timezone.utc)


def _load_sheet_job_ids(sheet_name: str, min_sheet_ids: int) -> set[str]:
    client = authenticate()
    worksheet = _sheets_api_call(client.open, sheet_name).sheet1
    rows = _sheets_api_call(worksheet.get_all_values)
    if not rows:
        raise RuntimeError(f"Sheet '{sheet_name}' is empty; refusing cleanup.")

    col_map = {_normalize_header(h): i for i, h in enumerate(rows[0])}
    job_id_idx = col_map.get("job_id")
    if job_id_idx is None:
        raise RuntimeError(
            f"Sheet '{sheet_name}' is missing required Job_ID column; refusing cleanup."
        )

    sheet_ids: set[str] = set()
    invalid = 0
    for row in rows[1:]:
        value = row[job_id_idx].strip() if job_id_idx < len(row) else ""
        if not value:
            continue
        if not JOB_ID_RE.match(value):
            invalid += 1
            continue
        sheet_ids.add(value)

    if len(sheet_ids) < min_sheet_ids:
        raise RuntimeError(
            f"Only {len(sheet_ids)} valid Sheet Job_IDs found (< {min_sheet_ids}); "
            "refusing cleanup because the Sheet read may be incomplete or corrupt."
        )
    if invalid:
        log.warning(f"Skipped {invalid} invalid Sheet Job_ID value(s)")
    return sheet_ids


def _list_local_folders(root: Path) -> tuple[list[LocalFolder], int]:
    if not root.exists():
        log.warning(f"Local applications root does not exist: {root}")
        return [], 0
    root_resolved = root.resolve()
    folders: list[LocalFolder] = []
    skipped_invalid = 0
    for path in root.iterdir():
        if not path.is_dir():
            continue
        job_id = _folder_job_id(path.name)
        if job_id is None:
            skipped_invalid += 1
            continue
        # Only direct children of the configured applications root are eligible.
        if path.resolve().parent != root_resolved:
            skipped_invalid += 1
            continue
        folders.append(LocalFolder(path=path, job_id=job_id, modified_at=_local_modified_at(path)))
    return folders, skipped_invalid


def _list_drive_folders() -> list[DriveFolder]:
    from execution.drive_upload import list_application_folders

    folders: list[DriveFolder] = []
    invalid = 0
    for raw in list_application_folders():
        job_id = _folder_job_id(str(raw.get("name", "")))
        if job_id is None:
            invalid += 1
            continue
        folders.append(
            DriveFolder(
                raw=raw,
                job_id=job_id,
                modified_at=_parse_drive_time(raw.get("modifiedTime")),
            )
        )
    if invalid:
        log.warning(f"Skipped {invalid} Drive folder(s) with invalid Job_ID prefix")
    return folders


def _split_orphans(
    folders: list[LocalFolder] | list[DriveFolder],
    sheet_ids: set[str],
    grace_cutoff: datetime,
) -> tuple[list[Any], list[Any]]:
    candidates: list[Any] = []
    in_grace: list[Any] = []
    for folder in folders:
        if folder.job_id in sheet_ids:
            continue
        # If mtime cannot be read, protect the folder rather than deleting it.
        if folder.modified_at is None or folder.modified_at >= grace_cutoff:
            in_grace.append(folder)
            continue
        candidates.append(folder)
    return candidates, in_grace


def _safety_abort_reasons(
    *,
    label: str,
    candidates: int,
    total: int,
    max_delete_pct: float,
    max_delete_count: int,
    force: bool,
) -> list[str]:
    reasons: list[str] = []
    pct = candidates / max(total, 1)
    if candidates == 0:
        return reasons
    if pct > max_delete_pct and not force:
        reasons.append(
            f"{label}: candidate share {pct:.1%} exceeds --max-delete-pct {max_delete_pct:.1%}"
        )
    if candidates > max_delete_count and not force:
        reasons.append(
            f"{label}: candidate count {candidates} exceeds --max-delete-count {max_delete_count}"
        )
    return reasons


def _safe_delete_local(folder: LocalFolder, root: Path) -> str | None:
    root_resolved = root.resolve()
    target = folder.path.resolve()
    if target.parent != root_resolved:
        return f"Refusing to delete non-direct child: {target}"
    if not folder.path.name.startswith("J-"):
        return f"Refusing to delete non-application folder: {target}"
    try:
        _make_writable_tree(target)
        shutil.rmtree(target, onerror=_rmtree_onerror)
        log.info(f"[local-delete] deleted {folder.path.name}")
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _make_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    except OSError:
        pass


def _make_writable_tree(root: Path) -> None:
    for current_root, dirs, files in os.walk(root, topdown=False):
        current = Path(current_root)
        for name in files:
            _make_writable(current / name)
        for name in dirs:
            _make_writable(current / name)
    _make_writable(root)


def _rmtree_onerror(func, path: str, _exc_info) -> None:
    _make_writable(Path(path))
    func(path)


def _names(items: list[Any], limit: int, *, local: bool) -> list[str]:
    if local:
        return [item.path.name for item in items[:limit]]
    return [str(item.raw.get("name", item.raw.get("id", "?"))) for item in items[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    ap.add_argument(
        "--targets",
        choices=("local", "drive", "both"),
        default="both",
        help="Which folder stores to inspect/delete (default: both).",
    )
    ap.add_argument("--local-root", type=Path, default=DEFAULT_LOCAL_ROOT)
    ap.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY)
    ap.add_argument("--grace-hours", type=int, default=24,
                    help="Protect folders modified within the last N hours (default: 24).")
    ap.add_argument("--min-sheet-ids", type=int, default=50,
                    help="Refuse cleanup if fewer valid Sheet Job_IDs are loaded (default: 50).")
    ap.add_argument("--max-delete-pct", type=float, default=0.25,
                    help="Refuse apply if candidates exceed this share of inspected folders (default: 0.25).")
    ap.add_argument("--max-delete-count", type=int, default=500,
                    help="Refuse apply if candidates exceed this count (default: 500).")
    ap.add_argument("--force", action="store_true",
                    help="Bypass max-delete-pct/count guards. Does not bypass empty/corrupt Sheet guard.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete candidates. Without this, the run is dry-run only.")
    ap.add_argument("--yes", action="store_true",
                    help="Required together with --apply for any deletion.")
    ap.add_argument("--sample", type=int, default=20,
                    help="Number of candidate names to print per target (default: 20).")
    args = ap.parse_args()

    if args.apply and not args.yes:
        log.error("Refusing to mutate: pass both --apply and --yes.")
        return 2
    if not 0 < args.max_delete_pct <= 1:
        log.error("--max-delete-pct must be in the range (0, 1].")
        return 2
    if args.grace_hours < 0:
        log.error("--grace-hours must be >= 0.")
        return 2

    target_local = args.targets in {"local", "both"}
    target_drive = args.targets in {"drive", "both"}
    mode = "APPLY" if args.apply else "DRY RUN"
    log.info(f"Mode: {mode}; targets={args.targets}; grace_hours={args.grace_hours}")

    errors: list[dict[str, str]] = []
    local_deleted = 0
    drive_deleted = 0
    drive_failed = 0

    try:
        sheet_ids = _load_sheet_job_ids(args.sheet_name, args.min_sheet_ids)
    except Exception as exc:
        log.error(f"Sheet load failed: {type(exc).__name__}: {exc}")
        write_run_summary(
            args.summary_path,
            stage="cleanup_orphan_application_folders",
            successful=0,
            failed=1,
            errors=[{"scope": "sheet", "error": f"{type(exc).__name__}: {exc}"}],
            apply=args.apply,
            targets=args.targets,
        )
        return 1

    log.info(f"Loaded {len(sheet_ids)} valid Sheet Job_IDs")
    grace_cutoff = datetime.now(timezone.utc) - timedelta(hours=args.grace_hours)

    local_total = local_candidates_count = local_in_grace_count = 0
    drive_total = drive_candidates_count = drive_in_grace_count = 0
    abort_reasons: list[str] = []
    local_candidates: list[LocalFolder] = []
    drive_candidates: list[DriveFolder] = []

    if target_local:
        local_folders, skipped_invalid = _list_local_folders(args.local_root)
        local_candidates, local_in_grace = _split_orphans(local_folders, sheet_ids, grace_cutoff)
        local_total = len(local_folders)
        local_candidates_count = len(local_candidates)
        local_in_grace_count = len(local_in_grace)
        log.info(
            f"Local: inspected={local_total}, candidates={local_candidates_count}, "
            f"in_grace_or_protected={local_in_grace_count}, skipped_invalid={skipped_invalid}"
        )
        if local_candidates:
            log.info(f"Local sample: {', '.join(_names(local_candidates, args.sample, local=True))}")
        abort_reasons.extend(
            _safety_abort_reasons(
                label="local",
                candidates=local_candidates_count,
                total=local_total,
                max_delete_pct=args.max_delete_pct,
                max_delete_count=args.max_delete_count,
                force=args.force,
            )
        )

    if target_drive:
        try:
            drive_folders = _list_drive_folders()
            drive_candidates, drive_in_grace = _split_orphans(drive_folders, sheet_ids, grace_cutoff)
            drive_total = len(drive_folders)
            drive_candidates_count = len(drive_candidates)
            drive_in_grace_count = len(drive_in_grace)
            log.info(
                f"Drive: inspected={drive_total}, candidates={drive_candidates_count}, "
                f"in_grace_or_protected={drive_in_grace_count}"
            )
            if drive_candidates:
                log.info(f"Drive sample: {', '.join(_names(drive_candidates, args.sample, local=False))}")
            abort_reasons.extend(
                _safety_abort_reasons(
                    label="drive",
                    candidates=drive_candidates_count,
                    total=drive_total,
                    max_delete_pct=args.max_delete_pct,
                    max_delete_count=args.max_delete_count,
                    force=args.force,
                )
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            log.error(f"Drive inspection failed: {msg}")
            errors.append({"scope": "drive", "error": msg})
            abort_reasons.append("drive: inspection failed")

    if abort_reasons:
        for reason in abort_reasons:
            log.error(f"[safety] {reason}")
        if args.apply:
            log.error("Apply refused by safety guard. Re-run with --force only after reviewing dry-run counts.")
        else:
            log.info("Dry-run only: safety guard would refuse apply without --force.")

    if args.apply and abort_reasons:
        log.info(
            "Summary: "
            f"local_candidates={local_candidates_count}, drive_candidates={drive_candidates_count}, "
            "local_deleted=0, drive_deleted=0, "
            f"failed={len(abort_reasons)}, safety_aborts={len(abort_reasons)}"
        )
        write_run_summary(
            args.summary_path,
            stage="cleanup_orphan_application_folders",
            successful=0,
            failed=len(abort_reasons),
            errors=[{"scope": "safety", "error": reason} for reason in abort_reasons],
            apply=args.apply,
            targets=args.targets,
            sheet_job_ids=len(sheet_ids),
            local={"inspected": local_total, "candidates": local_candidates_count, "in_grace": local_in_grace_count},
            drive={"inspected": drive_total, "candidates": drive_candidates_count, "in_grace": drive_in_grace_count},
            force=args.force,
        )
        return 2

    if args.apply and target_local:
        for folder in local_candidates:
            err = _safe_delete_local(folder, args.local_root)
            if err:
                errors.append({"scope": "local", "folder": str(folder.path), "error": err})
                log.warning(f"[local-delete] failed for {folder.path.name}: {err}")
            else:
                local_deleted += 1

    if args.apply and target_drive and drive_candidates:
        from execution.drive_upload import delete_drive_folders

        result = delete_drive_folders([folder.raw for folder in drive_candidates])
        drive_deleted = int(result["deleted"])
        drive_failed = int(result["failed"])
        for item in result.get("failed_items", []):
            errors.append({"scope": "drive", **item})

    if not args.apply:
        log.info("DRY RUN: no folders deleted. Add --apply --yes to mutate.")

    failed_count = len(errors)
    successful_count = local_deleted + drive_deleted if args.apply else 0
    log.info(
        "Summary: "
        f"local_candidates={local_candidates_count}, drive_candidates={drive_candidates_count}, "
        f"local_deleted={local_deleted}, drive_deleted={drive_deleted}, "
        f"failed={failed_count}, safety_aborts={len(abort_reasons)}"
    )
    write_run_summary(
        args.summary_path,
        stage="cleanup_orphan_application_folders",
        successful=successful_count,
        failed=failed_count,
        errors=errors,
        apply=args.apply,
        targets=args.targets,
        sheet_job_ids=len(sheet_ids),
        local={
            "inspected": local_total,
            "candidates": local_candidates_count,
            "in_grace_or_protected": local_in_grace_count,
            "deleted": local_deleted,
        },
        drive={
            "inspected": drive_total,
            "candidates": drive_candidates_count,
            "in_grace_or_protected": drive_in_grace_count,
            "deleted": drive_deleted,
            "failed": drive_failed,
        },
        force=args.force,
        safety_abort_reasons=abort_reasons,
        grace_hours=args.grace_hours,
        max_delete_pct=args.max_delete_pct,
        max_delete_count=args.max_delete_count,
    )

    if args.apply and failed_count:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
