"""Stage 4.5: contact discovery for APPLYING rows in the Google Sheet.

Runs as its own scheduled Modal function (pipeline_discover_contacts, Tue+Thu
06:00 UTC) — split out of pipeline_full so a slow run can't abort notify +
Drive mirror.

LISTING-ANCHORED waterfall: every contact written to the sheet must come from
the listing itself (its description text or its own URL), never from a generic
company-level lookup. The earlier 5-source waterfall (Impressum scrape,
Google/SerpAPI, SMTP pattern guess) was retired 2026-05-16 because those
sources produced "right company, wrong person" writes that broke follow-ups.

Sources (in order; first hit wins):

    1. Posting LLM extract — extract_contacts.py:extract_contacts_from_posting
       Operates on the JD text stored at scrape time (capped at 5000 chars,
       HTML stripped). Catches in-body contact blocks ("Ihre Ansprechpartnerin
       …"), signature lines, and explicit recruiter emails.

    2. Listing-page re-fetch — contact_scraper.py:fetch_listing_page_text
       When Source 1 finds nothing, re-fetch the listing's own URL with
       Playwright and run the same LLM extraction on the rendered visible
       text. Catches contact widgets, sidebar boxes, and mailto: hrefs that
       were stripped before the cached `description` was stored. Anchor is
       the listing URL itself, not the company root.

    3. NOT_FOUND — sheet flagged for manual fill.

The script ALWAYS exits 0 even if individual rows fail — the parent Modal pipeline
must never abort because contact discovery had a bad day. Per-row errors are
caught, logged, and reported in the final summary.

Cache:
    Results are cached in `.tmp/company_cache.json` under the same per-company
    key used by `web_search.research_company`, with these contact fields:
        contact_name, contact_email, contact_title, contact_honorific,
        contact_source, contact_confidence, contact_fetched_at
    A 30-day TTL ensures we re-discover when people switch jobs.

    A separate per-URL cache (`.tmp/listing_page_cache.json`) records the
    extraction *result* of Source 2 keyed by job URL (NOT the raw page text,
    to keep file size small). 30 days for hits, 7 days for misses — prevents
    re-hammering the same URL when a row stays in NOT_FOUND between runs.

Usage:
    python execution/discover_contacts.py --sheet-triggered           # cloud trigger
    python execution/discover_contacts.py --limit 3 --dry-run         # local smoke test
    python execution/discover_contacts.py --reset-cache --limit 1     # force rediscovery

Sheet columns written:
    Contact_Person      — "Frau Anna Müller" (honorific + name when known)
    Contact_Email       — "anna.mueller@example.ch" or empty
    Contact_Source      — Posting | Listing_Page | Discovered | Generic_Inbox | Constructed | NEEDS_MANUAL | NOT_FOUND
    Contact_Confidence  — high | medium | low | (empty)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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

# Row-level resume checkpoint: when a run times out mid-batch, the next run
# skips already-processed job_ids so SERP/LLM cost isn't paid twice. The
# checkpoint auto-resets when its started_at is >24 h old (new cycle).
CHECKPOINT_PATH = PROJECT_ROOT / ".tmp" / "discover_contacts_checkpoint.json"
CHECKPOINT_TTL_HOURS = 24
CHECKPOINT_FLUSH_EVERY = 10


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
    terminal_misses = {"NOT_FOUND", "NEEDS_MANUAL"}
    if not entry.get("contact_email") and entry.get("contact_source") not in terminal_misses:
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
# Row-level resume checkpoint
# ---------------------------------------------------------------------------

def _load_checkpoint(reset: bool = False) -> dict:
    """Return the active checkpoint or a fresh one if stale / missing / reset."""
    fresh = {"started_at": datetime.now().isoformat(timespec="seconds"), "completed": []}
    if reset or not CHECKPOINT_PATH.exists():
        return fresh
    try:
        data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"  Checkpoint read failed ({exc}); starting fresh")
        return fresh
    started_at = data.get("started_at")
    if not started_at:
        return fresh
    try:
        started_dt = datetime.fromisoformat(started_at)
    except ValueError:
        return fresh
    age_hours = (datetime.now() - started_dt).total_seconds() / 3600
    if age_hours > CHECKPOINT_TTL_HOURS:
        log.info(f"  Checkpoint stale ({age_hours:.1f}h > {CHECKPOINT_TTL_HOURS}h); starting fresh cycle")
        return fresh
    completed = data.get("completed") or []
    if not isinstance(completed, list):
        return fresh
    data["completed"] = list(completed)
    return data


def _save_checkpoint(checkpoint: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        CHECKPOINT_PATH.write_text(
            json.dumps(checkpoint, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning(f"  Checkpoint write failed: {exc}")


# ---------------------------------------------------------------------------
# Domain helpers + email-domain sanity check
# ---------------------------------------------------------------------------
#
# The listing-anchored waterfall (Posting → Listing_Page) can still extract an
# email that's on the wrong person — e.g. a hiring manager's personal Gmail
# that happens to appear in the posting signature. This validator rejects any
# extracted email whose domain is unrelated to the listing's company context.

_AGGREGATOR_HOSTS = (
    "linkedin.com", "indeed.com", "indeed.de", "indeed.ch", "glassdoor.com",
    "glassdoor.ch", "glassdoor.de",
    "stepstone.de", "stepstone.ch", "jobs.ch", "jobup.ch", "jobcloud.ch",
    "jobrapido.com", "ostjob.ch", "google.com", "myjob.ch", "talent.io",
    "hh.ru", "monster.com", "monster.de", "monster.ch", "xing.com",
    "experteer.com", "jobscout24.ch", "jobscout24.de",
    "jobagent.ch", "job.ch", "jobs4sales.ch", "workpool-jobs.ch",
    "job-room.ch", "jobijoba.ch", "topjobs.ch", "stellen-anzeiger.ch",
    "ictjobs.ch", "adzuna.ch", "efinancialcareers.ch", "expertini.com",
    "whatjobs.com", "jooble.org", "jobilize.com", "jobbern.ch",
    "swissdevjobs.ch", "itjob.ch", "it-jobs-switzerland.ch",
    "marketing-job.ch", "kundenberater-jobs.ch", "gamejobs.co",
    "recrute.ch", "emploi-en-suisse.ch",
)

# Free-mail providers — never accept these as a company contact. The LLM may
# correctly extract someone's personal address from a posting, but it's not
# the application channel we want to write to.
_FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.de", "outlook.ch",
    "hotmail.com", "hotmail.de", "hotmail.ch", "live.com", "live.de",
    "yahoo.com", "yahoo.de", "yahoo.ch", "yahoo.it", "yahoo.fr",
    "protonmail.com", "proton.me", "pm.me", "tutanota.com",
    "gmx.de", "gmx.ch", "gmx.at", "gmx.net", "gmx.com",
    "web.de", "t-online.de", "freenet.de", "arcor.de",
    "bluewin.ch", "hispeed.ch", "sunrise.ch", "swissonline.ch",
    "icloud.com", "me.com", "mac.com", "aol.com",
})

# Useful recruiter-related generic inboxes — when discovered on a company's
# contact surface, these are written with confidence="medium". Distinct from
# `_GENERIC_MAILBOX_PREFIXES` in extract_contacts.py (info@, contact@, sales@,
# marketing@, …) which are noise for our purpose.
_USEFUL_RECRUITER_INBOXES = (
    "careers@", "career@", "hr@", "jobs@", "job@", "recruiting@",
    "recruitment@", "recruiter@", "talent@", "talents@",
    "bewerbung@", "bewerbungen@", "personal@", "personalwesen@",
    "people@", "hiring@", "join@", "joinus@",
)


def _domain_from_url(url: str) -> str | None:
    """Extract a registrable-ish host from a URL string. Lower-cased, www-stripped."""
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return host or None
    except Exception:  # noqa: BLE001
        return None


def _looks_like_aggregator(domain: str | None) -> bool:
    if not domain:
        return False
    d = domain.lower().strip().rstrip(".")
    return any(d == host or d.endswith("." + host) for host in _AGGREGATOR_HOSTS)


def _resolve_company_domain_free(
    company: str,
    job_url: str,
    job_description: str,
) -> str | None:
    """SerpAPI-free company-domain resolution.

    Tries (in order):
      1. Job URL host, when not on an aggregator.
      2. Domains extracted from emails in the job description (company-name match).
      3. URL guesses (first/hyphenated/concatenated × .ch/.com/.de), validated
         with a lightweight HTTP fetch — first guess that returns non-empty
         body containing the company's first word wins.

    Returns the bare registrable host (e.g. 'acmecorp.ch') or None.
    """
    from execution.web_search import (
        _extract_company_domain_from_job_url,
        _extract_domain_from_emails,
        _guess_company_urls,
        fetch_page_text,
    )
    from execution.utils import strip_legal_suffixes

    # Step 1 — job URL host (free, instant)
    url_from_job = _extract_company_domain_from_job_url(job_url or "")
    if url_from_job:
        host = _domain_from_url(url_from_job)
        if host and not _looks_like_aggregator(host) and host not in _FREE_MAIL_DOMAINS:
            return host

    # Step 2 — emails in description (already does company-name matching)
    url_from_emails = _extract_domain_from_emails(job_description or "", company or "")
    if url_from_emails:
        host = _domain_from_url(url_from_emails)
        if host and not _looks_like_aggregator(host) and host not in _FREE_MAIL_DOMAINS:
            return host

    # Step 3 — URL guesses, validated with a small body fetch
    if not company:
        return None
    clean_company = strip_legal_suffixes(company).lower()
    company_tokens = [
        t for t in re.split(r"[^a-z0-9]+", clean_company)
        if len(t) >= 4
    ]
    if not company_tokens:
        return None
    primary_token = company_tokens[0]

    for guess in _guess_company_urls(company):
        guess_host = _domain_from_url(guess)
        if not guess_host or _looks_like_aggregator(guess_host) or guess_host in _FREE_MAIL_DOMAINS:
            continue
        try:
            body = fetch_page_text(guess, max_chars=1500)
        except Exception:  # noqa: BLE001
            body = ""
        if not body:
            continue
        # Require the company's primary word to appear in the body — otherwise
        # we'd accept parked domains / unrelated registrations.
        if primary_token in body.lower():
            log.info(f"  Source 3: domain resolved via URL guess (validated): {guess_host}")
            return guess_host

    return None


def _build_expected_domains(job: dict) -> set[str]:
    """Build the set of domains acceptable for an extracted contact email.

    Sources, in order of strength:
      1. The listing URL's host, when it's not an aggregator. This is the
         strongest anchor — same URL the listing came from.
      2. JD-body email domains (regex via _EMAIL_RE) — ONLY when the listing
         URL is an aggregator. On aggregators, the JD body is the only listing
         anchor we have. On a direct company URL we already trust the host, so
         we don't pollute the set with potentially-stale or anti-pattern email
         domains the JD might mention ("send CV NOT to old@oldco.com", parent-
         group footers, customer-success contact, etc.).

    Returns an empty set when no anchor is available. An empty expected set
    causes _email_domain_acceptable to reject everything — that's deliberate;
    we'd rather miss than write a contact we can't tie to the listing.
    """
    from execution.extract_contacts import _EMAIL_RE

    expected: set[str] = set()
    listing_host = _domain_from_url(job.get("url", "") or "")
    on_aggregator = _looks_like_aggregator(listing_host)
    if listing_host and not on_aggregator:
        expected.add(listing_host)

    if on_aggregator:
        description = job.get("description", "") or ""
        for email in _EMAIL_RE.findall(description):
            dom = email.rsplit("@", 1)[-1].lower().strip().rstrip(".") if "@" in email else ""
            if dom and dom not in _FREE_MAIL_DOMAINS and not _looks_like_aggregator(dom):
                expected.add(dom)
    return expected


def _email_domain_acceptable(email: str | None, expected: set[str]) -> bool:
    """True when `email`'s domain matches an expected anchor for this listing.

    Acceptance:
      - Free-mail providers are rejected unconditionally even when `expected`
        is non-empty — they're never an application contact.
      - Otherwise a domain matches when it's equal to OR a subdomain of an
        expected entry.

    The previous "parent of expected" branch (extracted=roche.com when
    expected=hr.roche.com) was dropped 2026-05-16 because it gave attackers
    a free pass through ATS provider domains: with `acme.wd3.myworkdayjobs.com`
    seeded into expected, a hallucinated `careers@myworkdayjobs.com` would
    have passed. JDs that cite a contact almost always include the apex
    domain in the body, so the apex lands in expected via the regex pass
    anyway — we don't need the parent rule.
    """
    if not isinstance(email, str):
        return False
    email = email.strip().lower()
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1].strip()
    if not domain or domain in _FREE_MAIL_DOMAINS:
        return False
    if not expected:
        return False
    for exp in expected:
        exp_clean = exp.lower().strip()
        if not exp_clean:
            continue
        if domain == exp_clean or domain.endswith("." + exp_clean):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-URL listing-page cache (Source 2)
# ---------------------------------------------------------------------------
#
# Stores the *extraction result* (not the raw page text) keyed by job URL.
# Prevents re-hammering Playwright on the same URL when a row stays in
# NOT_FOUND between Tue/Thu runs.
#
# Schema:
#   { url: {"fetched_at": iso8601, "result": {contact_name, contact_email, ...}} }

LISTING_PAGE_CACHE_PATH = PROJECT_ROOT / ".tmp" / "listing_page_cache.json"
LISTING_PAGE_HIT_TTL_DAYS = 30
LISTING_PAGE_MISS_TTL_DAYS = 7


def _load_listing_page_cache() -> dict:
    if not LISTING_PAGE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(LISTING_PAGE_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"  Listing-page cache read failed ({exc}); starting fresh")
        return {}


def _save_listing_page_cache(cache: dict) -> None:
    LISTING_PAGE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        LISTING_PAGE_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning(f"  Listing-page cache write failed: {exc}")


def _listing_page_cached_result(cache: dict, url: str) -> dict | None:
    """Return cached Source 2 extraction if still fresh, else None."""
    entry = cache.get(url)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    result = entry.get("result")
    if not fetched_at or not isinstance(result, dict):
        return None
    try:
        fetched_dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return None
    age_days = (datetime.now() - fetched_dt).days
    ttl = LISTING_PAGE_HIT_TTL_DAYS if result.get("contact_email") else LISTING_PAGE_MISS_TTL_DAYS
    if age_days > ttl:
        return None
    return result


def _record_listing_page_result(cache: dict, url: str, result: dict) -> None:
    cache[url] = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "result": result,
    }


# ---------------------------------------------------------------------------
# Listing-anchored waterfall (Posting → Listing_Page → NOT_FOUND)
# ---------------------------------------------------------------------------

def discover_contacts(
    job: dict,
    openrouter_key: str | None,
    gemini_key: str | None,
    company_research: dict | None = None,  # unused after listing-anchored refactor; kept for caller compat
    browser=None,
    listing_page_cache: dict | None = None,
    cache: dict | None = None,
    company_key: str | None = None,
) -> dict:
    """Run the listing-anchored contact-discovery waterfall for a single job row.

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
    job_url = job.get("url", "") or ""

    from execution.extract_contacts import extract_contacts_from_posting

    # Pre-compute the domain anchor for the validator — used by both sources.
    expected_domains = _build_expected_domains(job)

    # Rejected emails are held here so Source 3 can retroactively re-validate
    # them once it discovers the company domain: Source 2 may find a real
    # person email (anna.mueller@acmecorp.ch) but expected_domains=∅ on an
    # aggregator listing, then Source 3 resolves acmecorp.ch and the rejected
    # email IS the right one.
    pending_emails: list[tuple[str, str]] = []  # (email, source_label)

    # ---- Source 1: posting LLM extract -----------------------------------
    try:
        s1 = extract_contacts_from_posting(description, openrouter_key, gemini_key)
        s1_email = s1.get("contact_email")
        if s1_email and _email_domain_acceptable(s1_email, expected_domains):
            out.update({
                "contact_name": s1.get("contact_name"),
                "contact_email": s1_email,
                "contact_title": s1.get("contact_title"),
                "contact_honorific": s1.get("contact_honorific"),
                "contact_source": "Posting",
                "contact_confidence": s1.get("confidence") or "medium",
            })
            log.info(f"  ✓ Source 1 (posting): {out['contact_email']}")
            return out
        if s1_email:
            # Domain mismatch — log loudly so we can tune the rules from real data.
            log.info(
                f"  Source 1 email rejected (domain not in expected set {expected_domains or '∅'}): "
                f"{s1_email}"
            )
            pending_emails.append((s1_email, "Posting"))
        else:
            s1_name = s1.get("contact_name")
            log.info(f"  Source 1: no email (name={s1_name!r}, expected_domains={expected_domains or '∅'})")
        # Carry partial info forward (name / honorific / title) for later sources
        for k in ("contact_name", "contact_title", "contact_honorific"):
            if s1.get(k) and not out[k]:
                out[k] = s1[k]
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  Source 1 failed: {type(exc).__name__}: {exc}")

    # ---- Source 2: re-fetch the listing's own URL ------------------------
    # The cached `description` field is HTML-stripped at scrape time, so contact
    # widgets, sidebar boxes, and mailto: hrefs are gone. Re-fetching the URL
    # restores the rendered DOM. Still listing-anchored — same URL as the row.
    if browser is not None and job_url:
        s2, expected_domains = _source2_with_aggregator_hop(
            job_url, browser, openrouter_key, gemini_key,
            extract_contacts_from_posting, listing_page_cache, expected_domains,
        )

        s2_email = s2.get("contact_email")
        if s2_email and _email_domain_acceptable(s2_email, expected_domains):
            out.update({
                "contact_name": s2.get("contact_name") or out["contact_name"],
                "contact_email": s2_email,
                "contact_title": s2.get("contact_title") or out["contact_title"],
                "contact_honorific": s2.get("contact_honorific") or out["contact_honorific"],
                "contact_source": "Listing_Page",
                "contact_confidence": s2.get("confidence") or "medium",
            })
            log.info(f"  ✓ Source 2 (listing page): {out['contact_email']}")
            return out
        if s2_email:
            log.info(
                f"  Source 2 email rejected (domain not in expected set {expected_domains or '∅'}): "
                f"{s2_email}"
            )
            pending_emails.append((s2_email, "Listing_Page"))
        else:
            s2_name = s2.get("contact_name")
            log.info(f"  Source 2: no email (name={s2_name!r}, expected_domains={expected_domains or '∅'})")
        # Carry partial info forward even on miss
        for k in ("contact_name", "contact_title", "contact_honorific"):
            if s2.get(k) and not out[k]:
                out[k] = s2[k]

    # ---- Pending-email rescue (no name required) -------------------------
    # Fires when Sources 1/2 found a useful email but expected_domains=∅
    # (typical aggregator listings). Accepts the rejected email when its
    # host word-matches the company name. Doesn't require an extracted
    # contact_name — this is the rescue path for rows where the page surfaces
    # a recruiter inbox like `careers@acmerobotics.co` with no person attached.
    rescue_result = _rescue_pending_emails(
        out=out,
        company=company,
        pending_emails=pending_emails,
        cache=cache,
        company_key=company_key or "",
        expected_domains=expected_domains,
    )
    if rescue_result is not None:
        expected_domains = rescue_result
        return out

    # ---- Source 3: pattern-anchored construction -------------------------
    # Only when Sources 1+2 surfaced a name but no validated email. Bails fast
    # on missing name/company. Per user direction: name-anchored only.
    if not out.get("contact_email") and out.get("contact_name") and company:
        try:
            expected_domains = _source3_pattern_construction(
                out=out,
                company=company,
                title=job.get("title", "") or "",
                company_key=company_key or "",
                cache=cache,
                expected_domains=expected_domains,
                pending_emails=pending_emails,
                job_url=job_url,
                job_description=description,
            )
            if out.get("contact_email"):
                return out
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Source 3 failed: {type(exc).__name__}: {exc}")

    # ---- NEEDS_MANUAL vs NOT_FOUND ---------------------------------------
    # Aggregator-walled rows (LinkedIn/Indeed/jobs.ch with no canonical hop)
    # are recoverable via 30s of manual click-through — surface them with a
    # distinct flag so the user can filter the sheet and hand-fill the top
    # rows. Direct-host failures (apply-button-only company pages, ATS
    # widgets) stay NOT_FOUND — no manual rescue path there either.
    listing_host = _domain_from_url(job_url)
    if _looks_like_aggregator(listing_host):
        out["contact_source"] = "NEEDS_MANUAL"
        log.info(f"  ⚑ Aggregator listing for {company} — flagging NEEDS_MANUAL ({listing_host})")
    else:
        log.info(f"  ✗ All sources missed for {company} — flagging NOT_FOUND")
    return out


def _source2_with_aggregator_hop(
    job_url: str,
    browser,
    openrouter_key: str | None,
    gemini_key: str | None,
    extract_fn,
    listing_page_cache: dict | None,
    expected_domains: set[str],
) -> tuple[dict, set[str]]:
    """Source 2 with one fall-through hop for aggregator listings.

    1. Try the listing URL directly (with cache).
    2. If that misses AND the original URL is on an aggregator host, look for
       the 'Apply on company site' link, follow it once, re-extract.

    The canonical URL is cached separately from the aggregator URL — both are
    legitimate keys, both deserve their own TTL.

    Returns: (extraction_result, expected_domains).
      `expected_domains` is the caller's set augmented with the canonical
      host when we hopped. Lets the caller validate emails discovered on the
      canonical page against that page's own host.
    """
    cached = (
        _listing_page_cached_result(listing_page_cache, job_url)
        if listing_page_cache is not None else None
    )
    if cached is not None:
        s2 = cached
        log.info(f"  Source 2 cache hit ({'email' if s2.get('contact_email') else 'miss'})")
    else:
        s2 = _run_listing_page_source(
            job_url, browser, openrouter_key, gemini_key, extract_fn,
        )
        if listing_page_cache is not None:
            _record_listing_page_result(listing_page_cache, job_url, s2)

    # If the first hop produced a usable + domain-acceptable email, we're done.
    s2_email = s2.get("contact_email")
    if s2_email and _email_domain_acceptable(s2_email, expected_domains):
        return s2, expected_domains

    # Aggregator fallthrough — try one hop to the canonical apply URL.
    original_host = _domain_from_url(job_url)
    if not _looks_like_aggregator(original_host):
        return s2, expected_domains

    if s2_email:
        log.info(
            f"  Source 2 first-hop email failed validation; attempting canonical hop: {s2_email}"
        )

    try:
        from execution.contact_scraper import find_canonical_apply_url
        canonical = find_canonical_apply_url(job_url, browser)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  canonical-apply hop crashed: {type(exc).__name__}: {exc}")
        return s2, expected_domains
    if not canonical:
        log.info(f"  Aggregator hop: no canonical apply link found on {original_host}")
        return s2, expected_domains
    if canonical == job_url:
        log.info(f"  Aggregator hop: canonical URL == listing URL (skipped)")
        return s2, expected_domains

    log.info(f"  Source 2 (aggregator → canonical): {canonical}")
    # Augment the expected-domain set with the canonical URL's host so the
    # canonical page can validate emails on its own domain.
    augmented = set(expected_domains)
    canonical_host = _domain_from_url(canonical)
    if canonical_host and not _looks_like_aggregator(canonical_host):
        augmented.add(canonical_host)

    cached_c = (
        _listing_page_cached_result(listing_page_cache, canonical)
        if listing_page_cache is not None else None
    )
    if cached_c is not None:
        s2_canon = cached_c
        log.info(f"  Canonical cache hit ({'email' if s2_canon.get('contact_email') else 'miss'})")
    else:
        s2_canon = _run_listing_page_source(
            canonical, browser, openrouter_key, gemini_key, extract_fn,
        )
        if listing_page_cache is not None:
            _record_listing_page_result(listing_page_cache, canonical, s2_canon)

    # Prefer the canonical hit if it produced an email, otherwise fall back to s2.
    if s2_canon.get("contact_email"):
        return s2_canon, augmented
    return s2, augmented


def _run_listing_page_source(
    job_url: str,
    browser,
    openrouter_key: str | None,
    gemini_key: str | None,
    extract_fn,
) -> dict:
    """Re-fetch the job posting URL and run posting extraction on the rendered text.

    Returns the same dict shape as `extract_contacts_from_posting` (never raises).
    Empty text or fetch failure → all-None dict, which is then cached as a miss.
    """
    empty = {
        "contact_name": None,
        "contact_email": None,
        "contact_title": None,
        "contact_honorific": None,
        "confidence": "low",
    }
    try:
        from execution.contact_scraper import fetch_listing_page_text
        page_text = fetch_listing_page_text(job_url, browser=browser, timeout_ms=20_000)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  Source 2 fetch crashed: {type(exc).__name__}: {exc}")
        return empty
    if not page_text or len(page_text) < 80:
        log.info(f"  Source 2: no usable text from {job_url}")
        return empty
    log.info(f"  Source 2: fetched {len(page_text)} chars from {job_url}")
    try:
        s2 = extract_fn(page_text, openrouter_key, gemini_key)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  Source 2 extraction crashed: {type(exc).__name__}: {exc}")
        return empty
    # Normalize to the same keys the cache expects
    return {
        "contact_name": s2.get("contact_name"),
        "contact_email": s2.get("contact_email"),
        "contact_title": s2.get("contact_title"),
        "contact_honorific": s2.get("contact_honorific"),
        "confidence": s2.get("confidence") or "medium",
    }


# ---------------------------------------------------------------------------
# Source 3 — pattern-anchored email construction
# ---------------------------------------------------------------------------
# Only fires when Sources 1+2 returned a name but no validated email. Uses
# web search to find the company's canonical domain, then scans impressum /
# contact / home pages for same-domain emails. Routes by confidence:
#   Discovered (the name's own email is on the page) → high
#   Generic_Inbox (careers@ / hr@ / jobs@ / …)      → medium
#   Constructed (pattern inferred from a third-party email) → low

_PATTERN_CACHE_TTL_DAYS = 90
_PATTERN_CACHE_MISS_TTL_DAYS = 14


def _cached_company_pattern(cache: dict, company_key: str) -> dict | None:
    """Return cached {email_domain, email_pattern, …} if fresh, else None."""
    if not company_key:
        return None
    entry = cache.get(company_key)
    if not isinstance(entry, dict):
        return None
    fetched = entry.get("email_pattern_fetched_at")
    if not fetched:
        return None
    try:
        fetched_date = datetime.strptime(fetched, "%Y-%m-%d").date()
    except ValueError:
        return None
    age_days = (datetime.now().date() - fetched_date).days
    has_data = bool(entry.get("email_domain"))
    ttl = _PATTERN_CACHE_TTL_DAYS if has_data else _PATTERN_CACHE_MISS_TTL_DAYS
    if age_days > ttl:
        return None
    return {
        "email_domain": entry.get("email_domain"),
        "email_pattern": entry.get("email_pattern"),
        "pattern_source_url": entry.get("pattern_source_url"),
    }


def _record_company_pattern(cache: dict, company_key: str, domain: str | None,
                            pattern: str | None, source_url: str | None) -> None:
    """Persist domain + pattern + ts on the per-company cache entry.

    Reuses the existing per-company cache file (`.tmp/company_cache.json`).
    Coexists with the contact fields written by _update_cache — same entry,
    different keys.
    """
    if not company_key:
        return
    entry = cache.setdefault(company_key, {})
    entry["email_domain"] = domain
    entry["email_pattern"] = pattern
    entry["pattern_source_url"] = source_url
    entry["email_pattern_fetched_at"] = datetime.now().strftime("%Y-%m-%d")


def _classify_company_emails(emails: list[str]) -> tuple[list[str], list[str]]:
    """Split same-domain emails into (person_specific, useful_generic).

    Drops `_USEFUL_RECRUITER_INBOXES` from person_specific so callers don't
    mistake `careers@x.com` for a real person. Drops `_GENERIC_MAILBOX_PREFIXES`
    (info@, contact@, sales@, …) entirely — those aren't useful for follow-up.
    """
    from execution.extract_contacts import _GENERIC_MAILBOX_PREFIXES
    person_specific: list[str] = []
    useful_generic: list[str] = []
    for raw in emails:
        if not raw or "@" not in raw:
            continue
        e = raw.lower()
        if any(e.startswith(p) for p in _USEFUL_RECRUITER_INBOXES):
            useful_generic.append(e)
            continue
        if any(e.startswith(p) for p in _GENERIC_MAILBOX_PREFIXES):
            continue  # harmless-generic — discard
        # Anything left is "person-specific" (or at least specific enough to
        # use for pattern inference).
        person_specific.append(e)
    return person_specific, useful_generic


def _email_matches_name(email: str, first: str, last: str) -> bool:
    """True if the email's local-part recognizably contains both first and last.

    Used to upgrade a discovered email to confidence=high when it's
    self-evidently the extracted person's address.
    """
    if not email or "@" not in email or not first or not last:
        return False
    local = email.split("@", 1)[0].lower()
    f = first.lower()
    l = last.lower()
    if f in local and l in local:
        return True
    # Initial + last (jdoe / j.doe) — still safe enough to call a match
    if local.startswith(f[0] + l) or local.startswith(f[0] + "." + l):
        return True
    return False


def _rescue_pending_emails(
    out: dict,
    company: str,
    pending_emails: list[tuple[str, str]] | None,
    cache: dict | None,
    company_key: str,
    expected_domains: set[str],
) -> set[str] | None:
    """Promote a Source 1/2 rejected email when its host shares a word with the
    company name, OR when it's a recruiter inbox on a non-aggregator domain.

    Runs BEFORE the name-gated Source 3 — so we can rescue rows where Sources
    1/2 found a legitimate company/recruiter email but `expected_domains` was
    empty (typical on aggregator listings).

    Returns the augmented expected_domains set on a hit (and mutates `out` in
    place to write contact_email/source/confidence). Returns None on miss.

    Confidence policy:
      - Person-specific email + domain word-matches company → high
        (deterministic match — e.g. anna.mueller@acmecorp.ch for "AcmeCorp AG")
      - Useful-generic inbox (careers@, hr@, etc.) + word-matches company → medium
        (recruiter-inbox pattern — we know it's a recruiter inbox, the domain
         word-matches, but the actual recipient is anonymous)
    """
    if out.get("contact_email") or not pending_emails or not company:
        return None
    from execution.utils import strip_legal_suffixes
    clean_company = strip_legal_suffixes(company or "").lower()
    company_tokens = [
        t for t in re.split(r"[^a-z0-9]+", clean_company)
        if len(t) >= 4  # avoid 3-char false positives like "ag"/"sa"
    ]
    if not company_tokens:
        return None

    for pending_email, pending_source in pending_emails:
        if "@" not in pending_email:
            continue
        local = pending_email.split("@", 1)[0].lower()
        pending_host = pending_email.split("@", 1)[-1].lower()
        if not pending_host or pending_host in _FREE_MAIL_DOMAINS or _looks_like_aggregator(pending_host):
            continue
        host_root = pending_host.split(".")[0] if "." in pending_host else pending_host
        if not any(tok in pending_host or host_root in tok for tok in company_tokens):
            continue

        is_recruiter_inbox = any(
            local.startswith(p.rstrip("@")) and (len(local) == len(p.rstrip("@")))
            for p in _USEFUL_RECRUITER_INBOXES
        )
        confidence = "medium" if is_recruiter_inbox else "high"
        out["contact_email"] = pending_email
        out["contact_source"] = pending_source  # Posting | Listing_Page
        out["contact_confidence"] = confidence
        log.info(
            f"  Rescued pending {pending_source} email via company-name match "
            f"({pending_host} ~ {company}, confidence={confidence}): {pending_email}"
        )
        if cache is not None:
            _record_company_pattern(cache, company_key, pending_host, None, None)
        return set(expected_domains) | {pending_host}

    return None


def _source3_pattern_construction(
    out: dict,
    company: str,
    title: str,
    company_key: str,
    cache: dict | None,
    expected_domains: set[str],
    pending_emails: list[tuple[str, str]] | None = None,
    job_url: str = "",
    job_description: str = "",
) -> set[str]:
    """Run Source 3. Mutates `out` in place. Returns possibly-augmented expected_domains.

    Bails early when:
      - no extracted name in `out` (gate per user requirement)
      - already have a validated email in `out`
      - company is unknown

    `pending_emails`: list of (email, source_label) that Sources 1/2 extracted
    but were rejected by the domain validator. Once Source 3 resolves the
    company domain we re-check these — most rejections happen because
    expected_domains was empty (aggregator listing), not because the email
    was bad.
    """
    if out.get("contact_email"):
        return expected_domains
    contact_name = out.get("contact_name")
    if not contact_name or not company:
        return expected_domains

    from execution.extract_contacts import (
        _split_name_for_email, _infer_email_pattern, _construct_email,
    )
    from execution.contact_scraper import find_company_contact_emails

    parts = _split_name_for_email(contact_name)
    if not parts:
        log.info(f"  Source 3: skipped (name '{contact_name}' can't be split into first/last)")
        return expected_domains
    first, last = parts

    # ---- Step 3a: resolve company domain (cache → free waterfall) ----
    # Free waterfall = job URL host → email-extracted domain → URL guess +
    # body-text validation. No SerpAPI; preserves the per-run scrape budget
    # for `scrape_jobs.py` (the primary credit consumer).
    cached_pattern = _cached_company_pattern(cache, company_key) if cache is not None else None
    domain: str | None = None
    pattern: str | None = None
    pattern_source_url: str | None = None

    if cached_pattern and cached_pattern.get("email_domain"):
        domain = cached_pattern["email_domain"]
        pattern = cached_pattern.get("email_pattern")
        pattern_source_url = cached_pattern.get("pattern_source_url")
        log.info(f"  Source 3: cached company domain={domain}, pattern={pattern}")
    else:
        try:
            domain = _resolve_company_domain_free(company, job_url, job_description)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"  Source 3: _resolve_company_domain_free crashed: {type(exc).__name__}: {exc}")
            domain = None
        if not domain:
            log.info(f"  Source 3: no company domain resolved via free waterfall for '{company}'")
            if cache is not None:
                _record_company_pattern(cache, company_key, None, None, None)
            return expected_domains
        log.info(f"  Source 3: company domain = {domain}")

    # Add the company domain to expected_domains so anything we write passes the validator.
    augmented = set(expected_domains) | {domain}

    # ---- Step 3b-pre: retroactively re-validate pending emails ----
    # Residual: a pending email whose host matches the free-resolver-discovered
    # domain but didn't trip `_rescue_pending_emails` (e.g. short company name
    # with no 4+ char tokens). Medium confidence — the free resolver's URL
    # guess + body-text validation is reasonably trustworthy but not as
    # deterministic as a name-token match. The earlier `_rescue_pending_emails`
    # that earns high confidence.
    if pending_emails:
        for pending_email, pending_source in pending_emails:
            if _email_domain_acceptable(pending_email, augmented):
                out["contact_email"] = pending_email
                out["contact_source"] = pending_source  # Posting | Listing_Page
                out["contact_confidence"] = "medium"
                log.info(
                    f"  Source 3: rescued pending {pending_source} email "
                    f"(matches Google-discovered domain {domain}, confidence=medium): {pending_email}"
                )
                if cache is not None:
                    _record_company_pattern(cache, company_key, domain, pattern, pattern_source_url)
                return augmented

    # ---- Step 3b: scan impressum/contact/home for same-domain emails ----
    # We re-scan even when cached pattern exists, because the actual emails
    # are needed for the Discovered/Generic priority check. The pattern cache
    # mainly saves the SERP credit + re-search, not the page scrape.
    try:
        all_emails = find_company_contact_emails(domain)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  Source 3: find_company_contact_emails crashed: {type(exc).__name__}: {exc}")
        all_emails = []

    if not all_emails:
        log.info(f"  Source 3: no emails found on {domain} contact surface")
        if cache is not None and not cached_pattern:
            # Cache the domain even with no pattern; saves a future SERP credit.
            _record_company_pattern(cache, company_key, domain, None, None)
        return augmented

    person_specific, useful_generic = _classify_company_emails(all_emails)
    log.info(
        f"  Source 3: found {len(person_specific)} person-specific + "
        f"{len(useful_generic)} useful-generic emails on {domain}"
    )

    # ---- Step 3c-i: Discovered — does any person-specific email match our name? ----
    for email in person_specific:
        if _email_matches_name(email, first, last):
            out["contact_email"] = email
            out["contact_source"] = "Discovered"
            out["contact_confidence"] = "high"
            log.info(f"  Source 3 Discovered: {email}")
            return augmented

    # ---- Step 3c-ii: Generic inbox (medium) — preferred over guessing ----
    # NOTE: per user direction (waterfall from highest), we check generic
    # before constructed. A real careers@ inbox is more reliable than a
    # guessed first.last that might never reach a human.
    if useful_generic:
        # Prefer the most "active" inbox: careers@ > jobs@ > recruiting@ > hr@ > others
        priority = ("careers@", "jobs@", "recruiting@", "recruitment@", "talent@",
                    "bewerbung@", "hr@", "people@", "hiring@")
        chosen = sorted(
            useful_generic,
            key=lambda e: next((i for i, p in enumerate(priority) if e.startswith(p)), 999),
        )[0]
        out["contact_email"] = chosen
        out["contact_source"] = "Generic_Inbox"
        out["contact_confidence"] = "medium"
        log.info(f"  Source 3 Generic_Inbox: {chosen}")
        return augmented

    # ---- Step 3c-iii: Constructed (low) — infer pattern + build candidate ----
    if not pattern and person_specific:
        # Try each person-specific email until one yields a usable pattern.
        for example in person_specific:
            inferred = _infer_email_pattern(example, domain)
            if inferred:
                pattern = inferred
                pattern_source_url = f"https://{domain}/"  # path not tracked precisely
                log.info(f"  Source 3: pattern inferred = {pattern} (from {example})")
                break

    if pattern:
        constructed = _construct_email(first, last, pattern, domain)
        if constructed:
            out["contact_email"] = constructed
            out["contact_source"] = "Constructed"
            out["contact_confidence"] = "low"
            log.info(f"  Source 3 Constructed: {constructed} (low confidence, pattern={pattern})")
            if cache is not None:
                _record_company_pattern(cache, company_key, domain, pattern, pattern_source_url)
            return augmented

    log.info(f"  Source 3: no usable pattern found for {domain}")
    if cache is not None and not cached_pattern:
        _record_company_pattern(cache, company_key, domain, None, None)
    return augmented


# ---------------------------------------------------------------------------
# Sheet integration
# ---------------------------------------------------------------------------

def _eligible_rows(
    rows: list[list[str]],
    header: list[str],
    sheet_triggered: bool,
    allowed_statuses: set[str] | None = None,
) -> list[dict]:
    """Filter sheet rows to those needing contact discovery.

    Eligibility:
      - URL is non-empty (we need a row identifier)
      - Contact_Email is empty (don't overwrite manually-set values)
      - Status filter:
          * `sheet_triggered=True` → only Status=APPLYING (cloud cron behavior)
          * `allowed_statuses` set  → only rows whose Status (case-insensitive) is in that set
          * neither                → no status filter (legacy permissive default)
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
        status_lower = status.lower()
        if sheet_triggered:
            if status_lower != "applying":
                continue
        elif allowed_statuses is not None:
            if status_lower not in allowed_statuses:
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
    parser.add_argument("--statuses", default="",
                        help="Comma-separated statuses to filter (case-insensitive). "
                             "Use `--statuses Applied` to backfill emailless Applied rows. "
                             "Default: no status filter (every row without Contact_Email is eligible). "
                             "Ignored if --sheet-triggered is set.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N eligible rows")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log discoveries without writing to the sheet")
    parser.add_argument("--reset-cache", action="store_true",
                        help="Ignore cached contact_* keys; force fresh discovery")
    args = parser.parse_args()

    allowed_statuses: set[str] | None
    if args.statuses.strip():
        allowed_statuses = {s.strip().lower() for s in args.statuses.split(",") if s.strip()}
        if args.sheet_triggered:
            log.warning("--statuses ignored when --sheet-triggered is set "
                        "(cron path enforces Status=APPLYING)")
    else:
        allowed_statuses = None

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
    eligible = _eligible_rows(all_rows[1:], header, args.sheet_triggered, allowed_statuses)

    # Load cache early so we can partition fresh rows ahead of cached-NOT_FOUND
    # retries. Without this, the first --limit rows are dominated by NOT_FOUND
    # entries (which never write Contact_Email, so they stay eligible until the
    # 30-day cache TTL expires) and fresh rows past position --limit never run.
    cache = _load_cache()
    if args.limit is not None and not args.reset_cache:
        from execution.utils import normalize_company_key
        fresh, retry = [], []
        for job in eligible:
            ck = normalize_company_key(job["company"]) if job["company"] else ""
            cached = _cached_contact(cache, ck) if ck else None
            if cached and cached.get("contact_source") in ("NOT_FOUND", "NEEDS_MANUAL"):
                retry.append(job)
            else:
                fresh.append(job)
        eligible = fresh + retry
        log.info(f"Partitioned eligible rows: {len(fresh)} fresh + {len(retry)} cached-NOT_FOUND retry")

    # Row-level resume: skip job_ids already processed in this 24h cycle.
    # Filter BEFORE the --limit slice so the budget burns un-processed rows.
    from execution.utils import generate_job_id
    checkpoint = _load_checkpoint(reset=args.reset_cache)
    completed_ids: set[str] = set(checkpoint.get("completed") or [])
    if completed_ids:
        before = len(eligible)
        eligible = [
            j for j in eligible
            if generate_job_id(j["title"], j["company"], j["url"]) not in completed_ids
        ]
        log.info(f"Checkpoint: {len(completed_ids)} completed this cycle, "
                 f"{before - len(eligible)} skipped, {len(eligible)} remaining")

    if args.limit is not None:
        eligible = eligible[: args.limit]

    log.info(f"Eligible rows: {len(eligible)}")
    if not eligible:
        return 0
    summary = {"hits": 0, "misses": 0, "errors": 0, "cached": 0}

    # Per-URL Source 2 cache (separate from the per-company cache above)
    listing_page_cache = {} if args.reset_cache else _load_listing_page_cache()

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
                    if cached and (
                        cached.get("contact_email")
                        or cached.get("contact_source") in ("NOT_FOUND", "NEEDS_MANUAL")
                    ):
                        log.info(f"  Cache hit ({cached.get('contact_source')})")
                        info = cached
                        summary["cached"] += 1
                    else:
                        info = _do_discover(job, ws_cache, openrouter_key, gemini_key,
                                            cache, company_key, browser, listing_page_cache)
                else:
                    info = _do_discover(job, ws_cache, openrouter_key, gemini_key,
                                        cache, company_key, browser, listing_page_cache)

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

                # Mark this row done so a future timeout doesn't re-process it.
                job_id = generate_job_id(job["title"], job["company"], job["url"])
                if job_id not in completed_ids:
                    completed_ids.add(job_id)
                    checkpoint["completed"] = sorted(completed_ids)
                    if len(completed_ids) % CHECKPOINT_FLUSH_EVERY == 0:
                        _save_checkpoint(checkpoint)

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
    _save_listing_page_cache(listing_page_cache)

    # Final checkpoint flush (covers the tail of rows since the last 10-row flush).
    _save_checkpoint(checkpoint)

    log.info("")
    log.info(f"Discovery complete: hits={summary['hits']}, misses={summary['misses']}, "
             f"cached={summary['cached']}, errors={summary['errors']}")
    log.info(f"Checkpoint: {len(completed_ids)} job_ids recorded at {CHECKPOINT_PATH}")
    return 0  # Never propagate failure


def _do_discover(
    job: dict,
    ws_cache: dict,
    openrouter_key: str | None,
    gemini_key: str | None,
    cache: dict,
    company_key: str,
    browser,
    listing_page_cache: dict,
) -> dict:
    """Run the waterfall for a single job and update the cache."""
    # research_company cache kept as a future-proof pass-through; unused after
    # the listing-anchored refactor but harmless and avoids a wider signature change.
    research = ws_cache.get(company_key) if company_key else None

    info = discover_contacts(
        job=job,
        openrouter_key=openrouter_key,
        gemini_key=gemini_key,
        company_research=research,
        browser=browser,
        listing_page_cache=listing_page_cache,
        cache=cache,
        company_key=company_key,
    )
    if company_key:
        _update_cache(cache, company_key, info)
    return info


if __name__ == "__main__":
    sys.exit(main())
