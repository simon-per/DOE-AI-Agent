"""Stage 4.5: contact discovery for APPLYING rows in the Google Sheet.

Runs after Stage 4 (cover letter) + Stage 5 (CV) generate documents and the row
status flips to APPLYING. Tries five free sources in order, stops at the first
that yields a contact email, and writes the result to the sheet so Stage 6
(follow-up) has someone to email and the cover letter knows whom to address.

Sources (high → low confidence):
    1. Posting LLM extract (extract_contacts.py:extract_contacts_from_posting)
    2. Impressum / Kontakt page scrape (contact_scraper.py:scrape_company_contacts)
    3. SerpAPI Google search (web_search.py:search_company_contacts)
    4. Email pattern + SMTP RCPT verify (email_verifier.py:verify_pattern)
    5. NOT_FOUND — sheet flagged for manual fill

The script ALWAYS exits 0 even if individual rows fail — the parent Modal pipeline
must never abort because contact discovery had a bad day. Per-row errors are
caught, logged, and reported in the final summary.

Cache:
    Results are cached in `.tmp/company_cache.json` under the same per-company
    key used by `web_search.research_company`, with five new optional fields:
        contact_name, contact_email, contact_title, contact_honorific,
        contact_source, contact_confidence, contact_fetched_at
    A 30-day TTL ensures we re-discover when people switch jobs.

Usage:
    python execution/discover_contacts.py --sheet-triggered           # cloud trigger
    python execution/discover_contacts.py --limit 3 --dry-run         # local smoke test
    python execution/discover_contacts.py --reset-cache --limit 1     # force rediscovery

Sheet columns written:
    Contact_Person      — "Frau Anna Müller" (honorific + name when known)
    Contact_Email       — "anna.mueller@example.ch" or empty
    Contact_Source      — Posting | Impressum | Google | Pattern | NOT_FOUND
    Contact_Confidence  — high | medium | low | (empty)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("discover_contacts")

SHEET_NAME = "Swiss Job Search Pipeline"
COMPANY_CACHE_PATH = PROJECT_ROOT / ".tmp" / "company_cache.json"
CACHE_TTL_DAYS = 30

# Per-row hard cap so a slow site / slow MX server can't stall the whole run
PER_ROW_BUDGET_SEC = 60


# ---------------------------------------------------------------------------
# Cache helpers (extends the schema used by web_search.research_company)
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if not COMPANY_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(COMPANY_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"  Cache read failed ({exc}); starting fresh")
        return {}


def _save_cache(cache: dict) -> None:
    COMPANY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        COMPANY_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning(f"  Cache write failed: {exc}")


def _cached_contact(cache: dict, company_key: str) -> dict | None:
    """Return cached contact info if fresh, else None."""
    entry = cache.get(company_key)
    if not isinstance(entry, dict):
        return None
    fetched = entry.get("contact_fetched_at")
    if not fetched:
        return None
    try:
        fetched_date = datetime.strptime(fetched, "%Y-%m-%d").date()
    except ValueError:
        return None
    age_days = (datetime.now().date() - fetched_date).days
    if age_days > CACHE_TTL_DAYS:
        return None
    if not entry.get("contact_email") and entry.get("contact_source") != "NOT_FOUND":
        # Don't cache misses for too long — partial cache hits should re-attempt
        return None if age_days > 7 else _project_contact_fields(entry)
    return _project_contact_fields(entry)


def _project_contact_fields(entry: dict) -> dict:
    """Pull the contact-only fields out of a cache entry."""
    return {
        "contact_name": entry.get("contact_name"),
        "contact_email": entry.get("contact_email"),
        "contact_title": entry.get("contact_title"),
        "contact_honorific": entry.get("contact_honorific"),
        "contact_source": entry.get("contact_source"),
        "contact_confidence": entry.get("contact_confidence"),
    }


def _update_cache(cache: dict, company_key: str, info: dict) -> None:
    """Merge contact info into the per-company cache entry."""
    entry = cache.setdefault(company_key, {})
    for key in ("contact_name", "contact_email", "contact_title", "contact_honorific",
                "contact_source", "contact_confidence"):
        entry[key] = info.get(key)
    entry["contact_fetched_at"] = datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def _domain_from_url(url: str) -> str | None:
    """Extract a registrable-ish domain from a URL string."""
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower().lstrip("www.")
        return host or None
    except Exception:  # noqa: BLE001
        return None


def _resolve_company_domain(job: dict, company_research: dict | None) -> str | None:
    """Pick the best company domain we know about. Used as input to scraper + verifier."""
    # 1) Cached `website` from research_company (most reliable)
    if company_research and company_research.get("website"):
        d = _domain_from_url(company_research["website"])
        if d and not _looks_like_aggregator(d):
            return d
    # 2) Email-domain extraction from the job description (already used by web_search)
    description = job.get("description", "")
    if description:
        try:
            from execution.web_search import _extract_domain_from_emails
            domain_url = _extract_domain_from_emails(description, job.get("company", ""))
            if domain_url:
                d = _domain_from_url(domain_url)
                if d:
                    return d
        except Exception:  # noqa: BLE001
            pass
    # 3) Job URL host (often the aggregator, last resort)
    job_url = job.get("url", "")
    d = _domain_from_url(job_url)
    if d and not _looks_like_aggregator(d):
        return d
    return None


_AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "stepstone.de", "stepstone.ch",
    "jobs.ch", "jobup.ch", "jobcloud.ch", "jobrapido.com", "ostjob.ch",
    "google.com", "myjob.ch", "talent.io", "hh.ru", "monster.com",
)


def _looks_like_aggregator(domain: str) -> bool:
    d = (domain or "").lower()
    return any(d.endswith(host) for host in _AGGREGATOR_HOSTS)


# ---------------------------------------------------------------------------
# 5-source waterfall
# ---------------------------------------------------------------------------

def discover_contacts(
    job: dict,
    openrouter_key: str | None,
    gemini_key: str | None,
    company_research: dict | None = None,
    browser=None,
) -> dict:
    """Run the 5-source contact-discovery waterfall for a single job row.

    Returns a normalized dict with the keys discover_contacts.py writes to the sheet.
    Always returns; never raises. Sources fail silently and we move on.
    """
    out = {
        "contact_name": None,
        "contact_email": None,
        "contact_title": None,
        "contact_honorific": None,
        "contact_source": "NOT_FOUND",
        "contact_confidence": "",
    }

    description = job.get("description", "") or ""
    company = job.get("company", "") or ""

    # ---- Source 1: posting LLM extract -----------------------------------
    try:
        from execution.extract_contacts import extract_contacts_from_posting
        s1 = extract_contacts_from_posting(description, openrouter_key, gemini_key)
        if s1.get("contact_email"):
            out.update({
                "contact_name": s1.get("contact_name"),
                "contact_email": s1.get("contact_email"),
                "contact_title": s1.get("contact_title"),
                "contact_honorific": s1.get("contact_honorific"),
                "contact_source": "Posting",
                "contact_confidence": s1.get("confidence") or "medium",
            })
            log.info(f"  ✓ Source 1 (posting): {out['contact_email']}")
            return out
        # Carry partial info forward (name / honorific / title) for later sources
        for k in ("contact_name", "contact_title", "contact_honorific"):
            if s1.get(k) and not out[k]:
                out[k] = s1[k]
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  Source 1 failed: {type(exc).__name__}: {exc}")

    # ---- Source 2: impressum / kontakt scrape ----------------------------
    domain = _resolve_company_domain(job, company_research)
    if domain:
        try:
            from execution.contact_scraper import scrape_company_contacts
            scraped = scrape_company_contacts(
                domain,
                openrouter_key=openrouter_key,
                gemini_key=gemini_key,
                browser=browser,
            )
            best = _pick_best_scraped(scraped)
            if best and best.get("email"):
                out.update({
                    "contact_name": best.get("name") or out["contact_name"],
                    "contact_email": best.get("email"),
                    "contact_title": best.get("title") or out["contact_title"],
                    "contact_source": _confidence_label_for_source(best.get("source")),
                    "contact_confidence": _confidence_for_source(best.get("source")),
                })
                log.info(f"  ✓ Source 2 ({best.get('source')}): {out['contact_email']}")
                return out
            # Fill name from scraped if posting didn't have one
            if best and best.get("name") and not out["contact_name"]:
                out["contact_name"] = best["name"]
                if best.get("title") and not out["contact_title"]:
                    out["contact_title"] = best["title"]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Source 2 failed: {type(exc).__name__}: {exc}")
    else:
        log.info("  Source 2 skipped: no usable company domain")

    # ---- Source 3: SerpAPI Google search ---------------------------------
    if domain or company:
        try:
            from execution.web_search import search_company_contacts
            hits = search_company_contacts(domain or "", company)
            best = _pick_best_scraped(hits)
            if best and best.get("email"):
                out.update({
                    "contact_name": best.get("name") or out["contact_name"],
                    "contact_email": best.get("email"),
                    "contact_title": best.get("title") or out["contact_title"],
                    "contact_source": "Google",
                    "contact_confidence": "low",
                })
                log.info(f"  ✓ Source 3 (google): {out['contact_email']}")
                return out
            if best and best.get("name") and not out["contact_name"]:
                out["contact_name"] = best["name"]
                if best.get("title") and not out["contact_title"]:
                    out["contact_title"] = best["title"]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Source 3 failed: {type(exc).__name__}: {exc}")

    # ---- Source 4: email pattern + SMTP RCPT -----------------------------
    if domain and out["contact_name"]:
        try:
            from execution.email_verifier import split_name, verify_pattern
            first, last = split_name(out["contact_name"])
            if first and last:
                email, conf = verify_pattern(first, last, domain)
                if email:
                    out.update({
                        "contact_email": email,
                        "contact_source": "Pattern",
                        "contact_confidence": conf or "low",
                    })
                    log.info(f"  ✓ Source 4 (pattern): {email}")
                    return out
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Source 4 failed: {type(exc).__name__}: {exc}")

    # ---- Source 5: NOT_FOUND ---------------------------------------------
    log.info(f"  ✗ All sources missed for {company} — flagging NOT_FOUND")
    return out


def _pick_best_scraped(items: list[dict]) -> dict | None:
    """Pick the highest-quality entry from a scraper / search result list."""
    if not items:
        return None
    # Priority: has email > impressum > kontakt > team > about
    label_rank = {"impressum": 0, "kontakt": 1, "team": 2, "karriere": 3, "about": 4, "google": 5}
    sorted_items = sorted(
        items,
        key=lambda d: (
            0 if d.get("email") else 1,
            label_rank.get((d.get("source") or "").lower(), 9),
        ),
    )
    return sorted_items[0]


def _confidence_label_for_source(label: str | None) -> str:
    """Map source label → user-visible Contact_Source."""
    return {
        "impressum": "Impressum",
        "kontakt": "Impressum",  # Functionally equivalent for our purposes
        "team": "Impressum",
        "about": "Impressum",
        "karriere": "Impressum",
        "google": "Google",
    }.get((label or "").lower(), "Impressum")


def _confidence_for_source(label: str | None) -> str:
    """Confidence rubric for scraped sources."""
    label_lower = (label or "").lower()
    if label_lower == "impressum":
        return "high"
    if label_lower in ("kontakt", "team"):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Sheet integration
# ---------------------------------------------------------------------------

def _eligible_rows(rows: list[list[str]], header: list[str], sheet_triggered: bool) -> list[dict]:
    """Filter sheet rows to those needing contact discovery.

    Eligibility:
      - Status = APPLYING (sheet_triggered) or any row with a Description (manual mode)
      - Contact_Email is empty (don't overwrite manually-set values)
      - URL is non-empty (we need a row identifier)
    """
    out = []
    col = {name: i for i, name in enumerate(header)}

    def cell(row: list[str], name: str) -> str:
        i = col.get(name)
        if i is None:
            return ""
        return row[i].strip() if i < len(row) else ""

    for row in rows:
        if not cell(row, "URL"):
            continue
        status = cell(row, "Status")
        if sheet_triggered and status.lower() != "applying":
            continue
        if cell(row, "Contact_Email"):
            continue  # Don't overwrite an existing email
        out.append({
            "url": cell(row, "URL"),
            "title": cell(row, "Title"),
            "company": cell(row, "Company"),
            "location": cell(row, "Location"),
            "description": cell(row, "Description"),
            "status": status,
        })
    return out


def _format_contact_person(name: str | None, honorific: str | None) -> str:
    """Build the display string written to Contact_Person."""
    if not name:
        return ""
    if honorific in ("Frau", "Herr", "Ms", "Mr"):
        return f"{honorific} {name}"
    return name


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Discover contact emails for APPLYING rows")
    parser.add_argument("--sheet-triggered", action="store_true",
                        help="Filter rows to Status=APPLYING (cloud-pipeline default)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N eligible rows")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log discoveries without writing to the sheet")
    parser.add_argument("--reset-cache", action="store_true",
                        help="Ignore cached contact_* keys; force fresh discovery")
    args = parser.parse_args()

    log.info(f"discover_contacts starting (sheet_triggered={args.sheet_triggered}, "
             f"limit={args.limit}, dry_run={args.dry_run}, reset_cache={args.reset_cache})")

    openrouter_key = os.getenv("OPEN_ROUTER_API_KEY")
    gemini_key = os.getenv("GOOGLE_AI_STUDIO_API_KEY")
    if not (openrouter_key or gemini_key):
        log.warning("No LLM keys present; only regex extraction will run")

    # Pull the sheet
    try:
        from execution.write_jobs_to_sheet import authenticate, _sheets_api_call, update_job_columns
        client = authenticate()
        ws = _sheets_api_call(client.open, SHEET_NAME).sheet1
        all_rows = _sheets_api_call(ws.get_all_values)
    except Exception as exc:  # noqa: BLE001
        log.error(f"Sheet read failed: {type(exc).__name__}: {exc}")
        return 0  # Always exit 0 — pipeline must continue

    if not all_rows or len(all_rows) < 2:
        log.info("Sheet empty or header-only; nothing to do")
        return 0

    header = [h.strip() for h in all_rows[0]]
    eligible = _eligible_rows(all_rows[1:], header, args.sheet_triggered)
    if args.limit is not None:
        eligible = eligible[: args.limit]

    log.info(f"Eligible rows: {len(eligible)}")
    if not eligible:
        return 0

    cache = _load_cache()
    summary = {"hits": 0, "misses": 0, "errors": 0, "cached": 0}

    # Try to set up a single Playwright browser to reuse across rows
    browser = None
    pw_ctx = None
    try:
        try:
            from playwright.sync_api import sync_playwright
            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=True)
            log.info("Playwright browser ready (will be reused across rows)")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"Playwright launch failed; Source 2 will skip: {type(exc).__name__}: {exc}")
            browser = None

        # Need company-research lookups — import once
        from execution.utils import normalize_company_key
        from execution.web_search import _load_company_cache as _wsc_load
        ws_cache = _wsc_load()

        for i, job in enumerate(eligible, start=1):
            log.info(f"\n[{i}/{len(eligible)}] {job['company']} — {job['title'][:50]}")
            company_key = normalize_company_key(job["company"]) if job["company"] else ""
            try:
                # Cache hit?
                if not args.reset_cache and company_key:
                    cached = _cached_contact(cache, company_key)
                    if cached and (cached.get("contact_email") or cached.get("contact_source") == "NOT_FOUND"):
                        log.info(f"  Cache hit ({cached.get('contact_source')})")
                        info = cached
                        summary["cached"] += 1
                    else:
                        info = _do_discover(job, ws_cache, openrouter_key, gemini_key,
                                            cache, company_key, browser)
                else:
                    info = _do_discover(job, ws_cache, openrouter_key, gemini_key,
                                        cache, company_key, browser)

                # Sheet write
                contact_person = _format_contact_person(
                    info.get("contact_name"), info.get("contact_honorific"),
                )
                updates = {
                    "Contact_Source": info.get("contact_source") or "NOT_FOUND",
                    "Contact_Confidence": info.get("contact_confidence") or "",
                }
                if contact_person:
                    updates["Contact_Person"] = contact_person
                if info.get("contact_email"):
                    updates["Contact_Email"] = info["contact_email"]
                    summary["hits"] += 1
                else:
                    summary["misses"] += 1

                if args.dry_run:
                    log.info(f"  [dry-run] would write: {updates}")
                else:
                    update_job_columns(SHEET_NAME, job["url"], updates)

            except Exception as exc:  # noqa: BLE001 — never let one row sink the whole run
                log.exception(f"  Row processing failed: {type(exc).__name__}: {exc}")
                summary["errors"] += 1
                continue

    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if pw_ctx is not None:
            try:
                pw_ctx.stop()
            except Exception:  # noqa: BLE001
                pass

    if not args.dry_run:
        _save_cache(cache)

    log.info("")
    log.info(f"Discovery complete: hits={summary['hits']}, misses={summary['misses']}, "
             f"cached={summary['cached']}, errors={summary['errors']}")
    return 0  # Never propagate failure


def _do_discover(
    job: dict,
    ws_cache: dict,
    openrouter_key: str | None,
    gemini_key: str | None,
    cache: dict,
    company_key: str,
    browser,
) -> dict:
    """Run the waterfall for a single job and update the cache."""
    # research_company cache for website / domain context (read-only here)
    research = ws_cache.get(company_key) if company_key else None

    info = discover_contacts(
        job=job,
        openrouter_key=openrouter_key,
        gemini_key=gemini_key,
        company_research=research,
        browser=browser,
    )
    if company_key:
        _update_cache(cache, company_key, info)
    return info


if __name__ == "__main__":
    sys.exit(main())
