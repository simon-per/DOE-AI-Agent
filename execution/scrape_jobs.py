"""
Stage 1: Scrape Swiss job listings from multiple sources.
Output: .tmp/raw_jobs.json

Sources:
- python-jobspy (Indeed, Glassdoor, Google; LinkedIn opt-in via DOE_ENABLE_LINKEDIN=1)
- SERP API Google Jobs — structured data, 500 calls/month (2 keys × 250)
- job-room.ch frontend search (Swiss government portal) [disabled: SSL issues]

Performance: Parallel execution with ThreadPoolExecutor (concurrent searches)

Deduplication:
- Within-run: URL + title|company matching
- Cross-run: Persistent seen_jobs.json tracks all previously scraped jobs
  by hash(title+company+url). New runs only output genuinely new listings.

SERP API budget: terms loaded from ABOUTME.md × pages = calls/run.
Smart pagination skips remaining pages if all results on a page are already seen.

Description fetching:
- After dedup, visits actual job URLs for listings with empty descriptions
- Rate-limited (2s per domain, 3 concurrent workers)
- Cache: .tmp/description_cache.json (skips blocked/error URLs for 7 days)
- LinkedIn URLs skipped (always login wall)
- Adds 'description_source' field: "original", "fetched", or "empty"

Usage:
    python execution/scrape_jobs.py                    # default: last 30 days
    python execution/scrape_jobs.py --hours-old 336    # last 2 weeks
    python execution/scrape_jobs.py --serp-pages 1     # save SERP budget
    python execution/scrape_jobs.py --no-serp           # skip SERP API entirely
    python execution/scrape_jobs.py --no-fetch-descriptions  # skip description fetching
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from execution.utils import generate_job_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
OUTPUT_FILE = TMP_DIR / "raw_jobs.json"
SEEN_JOBS_FILE = TMP_DIR / "seen_jobs.json"

def _get_search_terms() -> list[str]:
    """Load search terms from ABOUTME.md (single source of truth)."""
    try:
        from execution.profile_loader import load_profile
        return load_profile().search_terms
    except Exception:
        # Fallback if ABOUTME.md can't be parsed
        return [
            "Revenue Operations Specialist", "Sales Operations Specialist",
            "CRM Specialist", "Dynamics 365", "Sales Operations",
        ]

SEARCH_TERMS = _get_search_terms()

LOCATION = "Switzerland"
DEFAULT_HOURS_OLD = 72   # 3 days — Tue/Thu cadence; fresh > volume. Use --deep for 30-day backfill.
DEEP_HOURS_OLD = 720     # 30 days, opt-in via --deep

# Domains excluded from scrape — aggregators, redirect-only, paywalled.
# Matches exact domain OR any subdomain (e.g. "trabajo.org" catches "ch.trabajo.org").
BLOCKED_DOMAINS = frozenset({
    "jobleads.com",             # paid aggregator, can't apply without payment
    "trabajo.org",              # always redirects to jobleads.com
    "bebee.com",                # aggregator, hides company name → CV/cover letter can't be tailored
    "experteer.com",            # paid premium-subscription aggregator, effectively paywalled
    "experteer.ch",             # CH-TLD variant of experteer.com (paid)
    "talents.studysmarter.de",  # expired listings / lead grab, not real openings
    "rocken.jobs",              # lead-grab agency reposting other boards (per user policy)
    "talent.com",               # redirects to jobleads.com (paid funnel)
    "cosmoquick.com",           # user-excluded
})

# Junior / clerical / operative / call-center / retail-sales roles — out of scope.
# We target specialist/manager+ in CRM, RevOps, Sales Ops, Marketing Ops, BI.
JUNIOR_TOKENS = re.compile(
    r"\b("
    r"sachbearbeiter(?:in)?|junior|"
    r"trainee|volont(?:är|aer)(?:in)?|volontariat|"
    r"praktikant(?:in)?|praktikum|werkstudent(?:in)?|"
    r"lehrling|lehrstelle|"
    r"ausbildung|auszubildende(?:r)?|azubi|"
    r"hilfskraft|aushilfe|"
    r"inside sales(?: representative| rep)?|"
    r"sales development(?: representative| rep)?|sdr|"
    r"business development(?: representative| rep)?|bdr|"
    r"telesales|telefonist(?:in)?|"
    r"call ?cent(?:re|er)(?:[ -]agent)?|"
    r"verk(?:ä|ae)ufer(?:in)?|verkaufsberater(?:in)?|"
    r"au(?:ß|ss)endienst(?:mitarbeiter(?:in)?)?|"
    r"disponent(?:in)?|"
    r"assistent(?:in)?(?!\s+(?:der|des|cfo|ceo|cto|gl|gesch(?:ä|ae)ftsf(?:ü|ue)hr|vorstand))"
    r")\b",
    re.IGNORECASE,
)

# French / Italian title tokens — high-precision; words almost never appearing in DE/EN titles.
FR_IT_TOKENS = re.compile(
    r"\b("
    r"spécialiste|responsable|chargé(?:e)?|développeur(?:euse)?|opérateur(?:trice)?|"
    r"directeur|gestionnaire|"
    r"specialista|responsabile|sviluppatore|operatore|addetto(?:a)?|"
    r"impiegato(?:a)?|coordinatore"
    r")\b",
    re.IGNORECASE,
)

# Drop counters (logged at end of run).
_filter_stats_lock = threading.Lock()
_filter_stats: dict[str, int] = {"junior": 0, "fr_it": 0}


def _drop_by_title(title: str) -> str | None:
    """Return drop reason ('junior'|'fr_it') if title should be filtered, else None."""
    if not title:
        return None
    if JUNIOR_TOKENS.search(title):
        return "junior"
    if FR_IT_TOKENS.search(title):
        return "fr_it"
    return None


def _bump_filter(reason: str) -> None:
    with _filter_stats_lock:
        _filter_stats[reason] = _filter_stats.get(reason, 0) + 1


# --- SERP API configuration ---
SERPAPI_URL = "https://serpapi.com/search"
DEFAULT_SERP_PAGES = 2  # 20 results per term (10/page) — fresh > volume; page 3 was mostly stale tail

# Round-robin key rotation for 2× budget (500 calls/month)
_serpapi_keys: list[str] = []
for _key_name in ("SERPAPI_API_KEY", "SERPAPI_API_KEY2"):
    _k = os.getenv(_key_name, "").strip()
    if _k:
        _serpapi_keys.append(_k)

_serpapi_lock = threading.Lock()
_serpapi_call_count = 0

# Per-run counters for the niche boards. If a source ran against relevant terms
# but collected zero jobs overall, main() logs an error — turns silent schema
# regressions (Next.js path change, card markup change) into an actionable alert.
_niche_stats_lock = threading.Lock()
_niche_stats: dict[str, dict[str, int]] = {
    "jobs4sales": {"relevant_terms": 0, "jobs": 0},
    "ictjobs": {"relevant_terms": 0, "jobs": 0},
    "jobroom": {"relevant_terms": 0, "jobs": 0},
    "jobsch": {"relevant_terms": 0, "jobs": 0},
}


def _bump_niche_stats(source: str, jobs_count: int) -> None:
    """Record one relevant-term run for a niche source."""
    with _niche_stats_lock:
        s = _niche_stats[source]
        s["relevant_terms"] += 1
        s["jobs"] += jobs_count


def _get_serpapi_key() -> str | None:
    """Get next SERP API key using round-robin rotation."""
    global _serpapi_call_count
    if not _serpapi_keys:
        return None
    with _serpapi_lock:
        key = _serpapi_keys[_serpapi_call_count % len(_serpapi_keys)]
        _serpapi_call_count += 1
    return key


def scrape_jobspy(search_term: str, hours_old: int = DEFAULT_HOURS_OLD) -> list[dict]:
    """Scrape jobs using python-jobspy (Indeed, Glassdoor, Google).

    LinkedIn is opt-in via env var DOE_ENABLE_LINKEDIN=1 — disabled by default
    because cloud egress (Modal) gets 429-walled. SerpAPI Google-for-Jobs path
    still surfaces LinkedIn-aggregated listings either way.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("python-jobspy not installed. Run: pip install python-jobspy")
        return []

    sites = ["indeed", "glassdoor", "google"]
    if os.environ.get("DOE_ENABLE_LINKEDIN") == "1":
        sites.insert(1, "linkedin")

    jobs = []
    try:
        log.info(f"[jobspy] Searching: '{search_term}' in {LOCATION} (last {hours_old}h)")
        results = scrape_jobs(
            site_name=sites,
            search_term=search_term,
            location=LOCATION,
            results_wanted=25,
            hours_old=hours_old,
            country_indeed="Switzerland",
            linkedin_fetch_description=("linkedin" in sites),
        )

        for _, row in results.iterrows():
            job = normalize_job(
                title=str(row.get("title", "")),
                company=str(row.get("company", "")),
                location=str(row.get("location", "")),
                url=str(row.get("job_url", "")),
                description=str(row.get("description", "")),
                source=str(row.get("site", "jobspy")),
                date_posted=str(row.get("date_posted", "")) if row.get("date_posted") else None,
                salary=_extract_salary(row),
                employment_type=str(row.get("job_type", "")) if row.get("job_type") else None,
                search_term=search_term,
            )
            if job:
                jobs.append(job)

        log.info(f"[jobspy] Found {len(jobs)} jobs for '{search_term}'")

    except Exception as e:
        log.warning(f"[jobspy] Error searching '{search_term}': {e}")

    return jobs


def _extract_salary(row) -> str | None:
    """Extract salary info from jobspy row."""
    parts = []
    min_sal = row.get("min_amount")
    max_sal = row.get("max_amount")
    currency = row.get("currency", "")
    if min_sal is not None and not pd.isna(min_sal):
        parts.append(str(int(float(min_sal))))
    if max_sal is not None and not pd.isna(max_sal):
        parts.append(str(int(float(max_sal))))

    if parts:
        return f"{currency} {' - '.join(parts)}".strip()
    return None


_JOBROOM_API = "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search"
_JOBROOM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Origin": "https://www.job-room.ch",
    "Referer": "https://www.job-room.ch/job-search",
    "Content-Type": "application/json",
}


def scrape_jobroom_term(search_term: str, online_since_days: int, page_size: int = 50, max_pages: int = 3) -> list[dict]:
    """Scrape job-room.ch (Swiss federal RAV/SECO board) for one search term.

    The site's Angular SPA hits /jobadservice/api/jobAdvertisements/_search
    with a JSON body. The endpoint accepts unauthenticated POSTs, so we
    skip the browser entirely.
    """
    body = {
        "workloadPercentageMin": 10,
        "workloadPercentageMax": 100,
        "permanent": None,
        "companyName": None,
        "onlineSince": online_since_days,
        "displayRestricted": False,
        "professionCodes": [],
        "keywords": [search_term],
        "communalCodes": [],
        "cantonCodes": [],
    }
    jobs: list[dict] = []
    for page_idx in range(max_pages):
        params = {"page": page_idx, "size": page_size, "sort": "date_desc"}
        try:
            resp = requests.post(_JOBROOM_API, params=params, json=body, headers=_JOBROOM_HEADERS, timeout=20)
        except Exception as e:
            log.warning(f"[jobroom] request failed for '{search_term}' p{page_idx}: {e}")
            break
        if resp.status_code != 200:
            log.warning(f"[jobroom] HTTP {resp.status_code} for '{search_term}' p{page_idx}")
            break
        try:
            items = resp.json()
        except Exception as e:
            log.warning(f"[jobroom] non-JSON response for '{search_term}': {e}")
            break
        if not isinstance(items, list) or not items:
            break
        for item in items:
            job = _parse_jobroom_item(item, search_term=search_term)
            if job:
                jobs.append(job)
        if len(items) < page_size:
            break
    return jobs


def scrape_jobroom_all(search_terms: list[str], hours_old: int = DEFAULT_HOURS_OLD) -> list[dict]:
    """Scrape job-room.ch across all given search terms (sequential)."""
    online_since_days = max(1, (hours_old + 23) // 24)
    all_jobs: list[dict] = []
    for term in search_terms:
        term_jobs = scrape_jobroom_term(term, online_since_days)
        log.info(f"[jobroom] '{term}' → {len(term_jobs)} jobs")
        all_jobs.extend(term_jobs)
    _bump_niche_stats("jobroom", len(all_jobs))
    return all_jobs


def _parse_jobroom_item(item: dict, search_term: str | None = None) -> dict | None:
    """Parse a job-room.ch search result item.

    Schema (POST .../jobAdvertisements/_search):
        item.jobAdvertisement.jobContent.jobDescriptions[0].{title,description}
        item.jobAdvertisement.jobContent.company.name
        item.jobAdvertisement.jobContent.location.{city,cantonCode}
        item.jobAdvertisement.jobContent.externalUrl  (canonical employer/aggregator link)
        item.jobAdvertisement.publicationStartDate
    """
    try:
        ad = item.get("jobAdvertisement") if isinstance(item, dict) else None
        if not isinstance(ad, dict):
            return None
        content = ad.get("jobContent") or {}
        descs = content.get("jobDescriptions") or []
        if not descs:
            return None
        # Prefer DE description; fall back to first
        d = next((x for x in descs if (x.get("languageIsoCode") or "").lower() == "de"), descs[0])

        import html as _html
        def _strip(s: str) -> str:
            s = re.sub(r"<[^>]+>", "", s or "")
            return _html.unescape(s)

        title = _strip(d.get("title", ""))
        description = _strip(d.get("description", ""))

        company_obj = content.get("company") or {}
        company = company_obj.get("name", "") if isinstance(company_obj, dict) else str(company_obj)

        loc_list = content.get("location") or content.get("locations") or {}
        if isinstance(loc_list, list) and loc_list:
            loc_list = loc_list[0]
        if isinstance(loc_list, dict):
            city = loc_list.get("city", "")
            canton = loc_list.get("cantonCode", "")
            location = f"{city}, {canton}" if city and canton else (city or canton or "")
        else:
            location = str(loc_list) if loc_list else ""

        # External URL is the canonical employer/aggregator link; fallback to job-room detail.
        external_url = content.get("externalUrl") or ""
        if external_url:
            url = external_url
        else:
            ad_id = ad.get("id") or item.get("id") or ""
            url = f"https://www.job-room.ch/job-search/{ad_id}" if ad_id else ""

        date_posted = ad.get("publicationStartDate") or ad.get("createdTime")

        emp = content.get("employment") or {}
        workload_max = emp.get("workloadPercentageMax") if isinstance(emp, dict) else None
        employment_type = f"{workload_max}%" if workload_max else None

        return normalize_job(
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            source="jobroom",
            date_posted=date_posted,
            salary=None,
            employment_type=employment_type,
            search_term=search_term,
        )
    except Exception as e:
        log.warning(f"[jobroom] Failed to parse item: {e}")
        return None


# ---------------------------------------------------------------------------
# Niche Swiss boards (jobchannel network) — added 2026-04-22.
# Direct listings on sales- and IT-specialized portals that complement the
# Indeed/LinkedIn-heavy coverage from jobspy + SerpAPI.
# ---------------------------------------------------------------------------

_NICHE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

# Search-term filter for niche boards. Boards are sales/IT-focused; running
# dev-only terms wastes requests. Substring match against lowered term.
_NICHE_RELEVANT_KEYWORDS = frozenset({
    "crm", "dynamics", "sales", "cpq", "marketing", "vertrieb", "verkauf",
    "berater", "salesforce", "hubspot", "revenue operations", "power bi",
    "business analyst", "business intelligence", "customer success",
    "e-commerce", "ecommerce", "application manager",
})


def _niche_term_is_relevant(search_term: str) -> bool:
    """Cheap filter: only hit niche boards for terms in their wheelhouse."""
    t = (search_term or "").lower()
    return any(kw in t for kw in _NICHE_RELEVANT_KEYWORDS)


def _extract_next_data(html: str) -> dict | None:
    """Parse the Next.js __NEXT_DATA__ JSON payload embedded in the HTML."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def scrape_jobs4sales(search_term: str, max_pages: int = 3) -> list[dict]:
    """Scrape jobs4sales.ch — sales-specialized board (jobchannel network).

    Listings are server-rendered inside a Next.js __NEXT_DATA__ JSON payload
    at props.initialState.public.jobs.jobs — no HTML scraping needed.
    """
    if not _niche_term_is_relevant(search_term):
        log.debug(f"[jobs4sales] skipping '{search_term}' — not in sales/CRM wheelhouse")
        return []

    jobs: list[dict] = []
    domain = "jobs4sales.ch"

    for page in range(1, max_pages + 1):
        _rate_limit_domain(domain)
        # jobs4sales does not URL-encode spaces the same way requests does,
        # but requests.get with params= is the safe choice.
        url = "https://jobs4sales.ch/en/jobs"
        try:
            resp = requests.get(
                url,
                params={"q": search_term, "page": page},
                headers=_NICHE_HEADERS,
                timeout=30,
            )
            if resp.status_code != 200:
                log.warning(f"[jobs4sales] HTTP {resp.status_code} for '{search_term}' page {page}")
                break
            data = _extract_next_data(resp.text)
            if not data:
                log.warning(f"[jobs4sales] no __NEXT_DATA__ for '{search_term}'")
                break
            try:
                state = data["props"]["initialState"]["public"]["jobs"]["jobs"]
            except (KeyError, TypeError):
                log.warning(f"[jobs4sales] unexpected payload shape for '{search_term}'")
                break

            page_jobs = state.get("jobs") or []
            if not page_jobs:
                break

            for item in page_jobs:
                norm = _parse_jobs4sales_item(item, search_term=search_term)
                if norm:
                    jobs.append(norm)

            total = state.get("resultCount") or 0
            page_size = state.get("pageSize") or 10
            if page * page_size >= total:
                break
        except Exception as e:
            log.warning(f"[jobs4sales] Error on '{search_term}' page {page}: {e}")
            break

    log.info(f"[jobs4sales] Found {len(jobs)} jobs for '{search_term}'")
    _bump_niche_stats("jobs4sales", len(jobs))
    return jobs


def _parse_jobs4sales_item(item: dict, search_term: str | None = None) -> dict | None:
    """Normalize a single jobs4sales listing from the Next.js payload."""
    import html as _html
    try:
        job_id = item.get("jobId")
        if not job_id:
            return None
        # JSON payload carries raw HTML entities (&ouml;, &amp;, ...) in plain-
        # text fields; unescape so downstream CV/cover-letter generation sees
        # real characters.
        title = _html.unescape(item.get("title") or "")
        company = _html.unescape(item.get("companyName") or "")
        location = _html.unescape(item.get("location") or "")
        date_posted = (item.get("datePosted") or "")[:10] or None  # ISO -> YYYY-MM-DD
        workload = item.get("workload") or None
        url = f"https://jobs4sales.ch/en/job/{job_id}"

        # Listing omits description — Phase 2 (fetch_descriptions) fills it in.
        return normalize_job(
            title=title,
            company=company,
            location=location,
            url=url,
            description="",
            source="jobs4sales",
            date_posted=date_posted,
            salary=None,
            employment_type=str(workload) if workload else None,
            search_term=search_term,
        )
    except Exception as e:
        log.warning(f"[jobs4sales] Failed to parse item: {e}")
        return None


# ictjobs.ch serves server-rendered listing cards with schema.org microdata.
# Each card is wrapped in: <div class="offer publish"> ... </div><!--/.offer -->
_ICTJOBS_CARD_RE = re.compile(
    r'<div class="offer publish">(.*?)</div>\s*<!--/\.offer\s*-->',
    re.DOTALL,
)
_ICTJOBS_TITLE_RE = re.compile(
    r'<h2 itemprop="title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
_ICTJOBS_COMPANY_RE = re.compile(
    r'<span class="author-text">\s*<a[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
_ICTJOBS_LOCATION_RE = re.compile(
    r'<span class="label-in">.*?<span>\s*<a[^>]*>\s*(.*?)\s*</a>',
    re.DOTALL,
)
_ICTJOBS_DATE_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def scrape_ictjobs(search_term: str) -> list[dict]:
    """Scrape ictjobs.ch — IT-specialized board (jobchannel network).

    Uses the ?fs= filter on the root URL. Response is HTML with listing cards
    wrapped in <div class="offer publish"> — parsed via compiled regex.
    """
    if not _niche_term_is_relevant(search_term):
        log.debug(f"[ictjobs] skipping '{search_term}' — not in IT/CRM wheelhouse")
        return []

    jobs: list[dict] = []
    domain = "ictjobs.ch"
    _rate_limit_domain(domain)

    try:
        resp = requests.get(
            "https://ictjobs.ch/",
            params={"fs": search_term},
            headers=_NICHE_HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            log.warning(f"[ictjobs] HTTP {resp.status_code} for '{search_term}'")
            return jobs

        cards = _ICTJOBS_CARD_RE.findall(resp.text)
        # Self-alarm: body contains listing markers but regex missed everything
        # → markup likely changed. Catch silent regression early.
        if not cards and 'class="offer publish"' in resp.text:
            log.warning(
                f"[ictjobs] card regex matched 0 blocks for '{search_term}' "
                "despite 'offer publish' present in body — HTML structure may have changed"
            )

        for card in cards:
            norm = _parse_ictjobs_card(card, search_term=search_term)
            if norm:
                jobs.append(norm)
    except requests.RequestException as e:
        log.warning(f"[ictjobs] Network error on '{search_term}': {e}")

    log.info(f"[ictjobs] Found {len(jobs)} jobs for '{search_term}'")
    _bump_niche_stats("ictjobs", len(jobs))
    return jobs


def _parse_ictjobs_card(card_html: str, search_term: str | None = None) -> dict | None:
    """Normalize a single <div class='offer publish'> card."""
    import html as _html
    try:
        m_title = _ICTJOBS_TITLE_RE.search(card_html)
        if not m_title:
            return None
        raw_url = m_title.group(1).strip()
        # Drop the ?fs=... search-filter suffix so the same job from different
        # search terms dedupes on URL within a run.
        url = raw_url.split("?", 1)[0]
        title = _html.unescape(re.sub(r"\s+", " ", m_title.group(2)).strip())

        m_company = _ICTJOBS_COMPANY_RE.search(card_html)
        company = _html.unescape(re.sub(r"\s+", " ", m_company.group(1)).strip()) if m_company else ""

        m_loc = _ICTJOBS_LOCATION_RE.search(card_html)
        location = _html.unescape(re.sub(r"\s+", " ", m_loc.group(1)).strip()) if m_loc else ""

        m_date = _ICTJOBS_DATE_RE.search(card_html)
        date_posted = m_date.group(1) if m_date else None

        return normalize_job(
            title=title,
            company=company,
            location=location,
            url=url,
            description="",  # Phase 2 fetches descriptions
            source="ictjobs",
            date_posted=date_posted,
            salary=None,
            employment_type=None,
            search_term=search_term,
        )
    except Exception as e:
        log.warning(f"[ictjobs] Failed to parse card: {e}")
        return None


# ---------------------------------------------------------------------------
# jobs.ch — largest CH job board. Public API at
# https://www.jobs.ch/api/v1/public/search?query=...&page=N (no auth).
# Response: {documents: [...], total_hits, num_pages, ...}.
# Document fields used: title, company_name, place, preview, publication_date,
# initial_publication_date, age (days), _links.detail_de.href.
# ---------------------------------------------------------------------------
_JOBSCH_API = "https://www.jobs.ch/api/v1/public/search"


def scrape_jobsch_term(search_term: str, max_age_days: int, max_pages: int = 5) -> list[dict]:
    """Scrape jobs.ch for one search term, dropping postings older than
    `max_age_days` (the API exposes per-doc `age` in days)."""
    jobs: list[dict] = []
    for page_idx in range(1, max_pages + 1):
        params = {"query": search_term, "page": page_idx}
        try:
            resp = requests.get(
                _JOBSCH_API,
                params=params,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=20,
            )
        except Exception as e:
            log.warning(f"[jobsch] request failed for '{search_term}' p{page_idx}: {e}")
            break
        if resp.status_code != 200:
            log.warning(f"[jobsch] HTTP {resp.status_code} for '{search_term}' p{page_idx}")
            break
        try:
            data = resp.json()
        except Exception as e:
            log.warning(f"[jobsch] non-JSON for '{search_term}': {e}")
            break

        docs = data.get("documents", [])
        if not docs:
            break

        all_too_old = True
        for doc in docs:
            age = doc.get("age")
            if isinstance(age, (int, float)) and age <= max_age_days:
                all_too_old = False
                job = _parse_jobsch_doc(doc, search_term=search_term)
                if job:
                    jobs.append(job)

        # Results are sorted recency-first; once an entire page is over the
        # age cutoff, deeper pages can't help.
        if all_too_old:
            break
        if page_idx >= data.get("num_pages", page_idx):
            break
    return jobs


def _parse_jobsch_doc(doc: dict, search_term: str | None = None) -> dict | None:
    try:
        title = doc.get("title", "") or ""
        company = doc.get("company_name", "") or ""
        location = doc.get("place", "") or ""
        preview = doc.get("preview", "") or ""

        links = doc.get("_links") or {}
        url = ""
        for key in ("detail_de", "detail_en", "detail_fr"):
            link = links.get(key)
            if isinstance(link, dict) and link.get("href"):
                url = link["href"]
                break
        if not url:
            slug = doc.get("slug") or doc.get("job_id")
            if slug:
                url = f"https://www.jobs.ch/de/stellenangebote/detail/{slug}/"

        date_posted = doc.get("publication_date") or doc.get("initial_publication_date")

        grades = doc.get("employment_grades") or []
        if isinstance(grades, list) and len(grades) >= 1:
            employment_type = f"{grades[0]}%" if len(grades) == 1 else f"{min(grades)}-{max(grades)}%"
        else:
            employment_type = None

        return normalize_job(
            title=title,
            company=company,
            location=location,
            url=url,
            description=preview,
            source="jobsch",
            date_posted=date_posted,
            salary=None,
            employment_type=employment_type,
            search_term=search_term,
        )
    except Exception as e:
        log.warning(f"[jobsch] failed to parse doc: {e}")
        return None


def scrape_jobsch_all(search_terms: list[str], hours_old: int = DEFAULT_HOURS_OLD) -> list[dict]:
    """Scrape jobs.ch across all given search terms (sequential)."""
    max_age_days = max(1, (hours_old + 23) // 24)
    all_jobs: list[dict] = []
    for term in search_terms:
        term_jobs = scrape_jobsch_term(term, max_age_days=max_age_days)
        log.info(f"[jobsch] '{term}' → {len(term_jobs)} jobs")
        all_jobs.extend(term_jobs)
    _bump_niche_stats("jobsch", len(all_jobs))
    return all_jobs


# ---------------------------------------------------------------------------
# Public ATS boards (Greenhouse / Lever) for known CH employers.
# Curated list at execution/data/ch_ats_employers.json. These are the cleanest
# possible source: structured JSON, dated, direct employer-controlled — no
# aggregator pollution, no lead-grab.
# ---------------------------------------------------------------------------
_ATS_EMPLOYERS_FILE = PROJECT_ROOT / "execution" / "data" / "ch_ats_employers.json"
_CH_LOCATION_TOKENS = (
    "switzerland", "schweiz", "suisse", "svizzera", " ch ", ", ch", "ch,",
    "zurich", "zürich", "zuerich", "geneva", "genève", "geneve", "basel",
    "bern", "berne", "lausanne", "lugano", "luzern", "lucerne", "winterthur",
    "st. gallen", "st gallen", "zug", "fribourg", "freiburg", "neuchâtel",
    "neuchatel", "thun", "biel", "bienne", "schaffhausen", "remote ch",
    "remote - switzerland", "emea (switzerland)",
)


def _is_ch_location(loc: str) -> bool:
    if not loc:
        return False
    s = f" {loc.lower()} "
    return any(tok in s for tok in _CH_LOCATION_TOKENS)


def _load_ats_employers() -> dict:
    if not _ATS_EMPLOYERS_FILE.exists():
        return {}
    try:
        with open(_ATS_EMPLOYERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"[ats] failed to load employer list: {e}")
        return {}


def scrape_greenhouse(slug: str, company: str) -> list[dict]:
    """Greenhouse Job Board API (public, unauthenticated)."""
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        resp = requests.get(url, timeout=20, headers={"Accept": "application/json"})
    except Exception as e:
        log.warning(f"[greenhouse] {company} request failed: {e}")
        return []
    if resp.status_code != 200:
        log.warning(f"[greenhouse] {company} HTTP {resp.status_code}")
        return []
    try:
        data = resp.json()
    except Exception as e:
        log.warning(f"[greenhouse] {company} non-JSON: {e}")
        return []
    jobs: list[dict] = []
    import html as _html
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        if not _is_ch_location(location):
            continue
        title = j.get("title", "") or ""
        content = j.get("content", "") or ""
        desc = _html.unescape(re.sub(r"<[^>]+>", " ", content))
        desc = re.sub(r"\s+", " ", desc).strip()
        norm = normalize_job(
            title=title,
            company=company,
            location=location,
            url=j.get("absolute_url", ""),
            description=desc,
            source="greenhouse",
            date_posted=j.get("updated_at") or j.get("created_at"),
            salary=None,
            employment_type=None,
        )
        if norm:
            jobs.append(norm)
    return jobs


def scrape_lever(slug: str, company: str) -> list[dict]:
    """Lever public postings API."""
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, timeout=20, headers={"Accept": "application/json"})
    except Exception as e:
        log.warning(f"[lever] {company} request failed: {e}")
        return []
    if resp.status_code != 200:
        log.warning(f"[lever] {company} HTTP {resp.status_code}")
        return []
    try:
        postings = resp.json()
    except Exception as e:
        log.warning(f"[lever] {company} non-JSON: {e}")
        return []
    jobs: list[dict] = []
    import html as _html
    for p in postings if isinstance(postings, list) else []:
        cats = p.get("categories") or {}
        location = cats.get("location", "") or ""
        if not _is_ch_location(location):
            continue
        body_html = (p.get("descriptionPlain") or p.get("description") or "")
        desc = _html.unescape(re.sub(r"<[^>]+>", " ", body_html))
        desc = re.sub(r"\s+", " ", desc).strip()
        created_ms = p.get("createdAt")
        try:
            from datetime import datetime as _dt, timezone as _tz
            date_posted = _dt.fromtimestamp(created_ms / 1000, tz=_tz.utc).isoformat() if created_ms else None
        except Exception:
            date_posted = None
        norm = normalize_job(
            title=p.get("text", "") or "",
            company=company,
            location=location,
            url=p.get("hostedUrl", "") or p.get("applyUrl", ""),
            description=desc,
            source="lever",
            date_posted=date_posted,
            salary=None,
            employment_type=cats.get("commitment"),
        )
        if norm:
            jobs.append(norm)
    return jobs


def scrape_ats_boards_all() -> list[dict]:
    """Iterate the curated CH ATS employer list (Greenhouse + Lever)."""
    cfg = _load_ats_employers()
    all_jobs: list[dict] = []
    for entry in cfg.get("greenhouse", []):
        slug = entry.get("slug")
        company = entry.get("company") or slug
        if not slug:
            continue
        gj = scrape_greenhouse(slug, company)
        log.info(f"[greenhouse] {company} → {len(gj)} CH jobs")
        all_jobs.extend(gj)
    for entry in cfg.get("lever", []):
        slug = entry.get("slug")
        company = entry.get("company") or slug
        if not slug:
            continue
        lj = scrape_lever(slug, company)
        log.info(f"[lever] {company} → {len(lj)} CH jobs")
        all_jobs.extend(lj)
    return all_jobs


def _try_extract_company(description: str) -> str:
    """
    Try to extract company name from job description when company field is empty.
    Uses common patterns in German/English/Italian job postings.
    Returns company name or empty string.
    """
    import re
    if not description or description.strip() in ("", "nan"):
        return ""

    # Pattern 1: "gehört [Company] zu den" / "[Company] ist ein"
    patterns = [
        # German: "gehört X zu", "Bei X arbeitest du", "X ist ein führendes"
        r"gehört\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß&.\-]+(?:\s+(?:AG|GmbH|SA|Sàrl|Ltd|Inc|Group|SE))?)\s+zu",
        r"(?:Bei|Für)\s+(?:der\s+|die\s+|das\s+)?([A-ZÄÖÜ][A-Za-zÄÖÜäöüß&.\-]+(?:\s+(?:AG|GmbH|SA|Sàrl|Ltd|Inc|Group|SE))?)\s+(?:arbeitest|arbeiten|bist|gestaltest|entwickelst)",
        r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß&.\-]+(?:\s+(?:AG|GmbH|SA|Sàrl|Ltd|Inc|Group|SE)))\s+(?:ist ein|sucht|bietet)",
        # English: "About [Company]", "[Company] is a leading"
        r"(?:About|Join)\s+([A-Z][A-Za-z&.\-]+(?:\s+(?:AG|GmbH|SA|Ltd|Inc|Group|SE))?)",
        r"([A-Z][A-Za-z&.\-]+(?:\s+(?:AG|GmbH|SA|Ltd|Inc|Group|SE)))\s+is\s+(?:a|an|the)\s+(?:leading|global|Swiss|innovative)",
        # Legal entity suffixes are strong signals
        r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß&.\- ]{2,30}(?:AG|GmbH|SA|Sàrl|Ltd|SE))",
    ]

    for pattern in patterns:
        match = re.search(pattern, description)
        if match:
            name = match.group(1).strip().rstrip(".")
            # Sanity: core name (without legal suffix) must be 3+ chars,
            # starts with uppercase, max 6 words (prevents capturing sentences)
            core_name = re.sub(r'\b(AG|GmbH|SA|Sàrl|Ltd|SE)\b', '', name).strip()
            if 3 <= len(core_name) <= 50 and name[0].isupper() and len(name.split()) <= 6:
                return name

    return ""


def _clean_url(url: str) -> str:
    """Normalize URL: strip tracking params, ensure https, strip trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    # Must start with http(s)
    if not url.startswith("http"):
        return ""
    # Strip tracking/analytics params
    if "?" in url:
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        tracking_keys = {k for k in params if
                         k.startswith(("utm_", "_ga", "_gid", "affiliate_", "partner_")) or
                         k in ("ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid",
                               "campaign", "medium")}
        for k in tracking_keys:
            del params[k]
        clean_query = urlencode(params, doseq=True)
        url = urlunparse(parsed._replace(query=clean_query))
        if url.endswith("?"):
            url = url[:-1]
    return url


# talent.com/LinkedIn/etc. append aggregator-UI annotations like
# "Zürich (+ 1 weiterer Standort)" / "Zurich (+ 2 other locations)" when a posting
# lists multiple cities. Those don't belong in a CV/cover letter header.
_LOCATION_MULTI_RE = re.compile(
    r"\s*\(\s*\+\s*\d+\s*(?:weitere[rn]?|andere[rn]?|other|more)\s+"
    r"(?:standort(?:e|en)?|ort(?:e|en)?|location[s]?)\s*\)\s*$",
    re.IGNORECASE,
)


def _clean_location(location: str) -> str:
    """Strip aggregator-UI annotations like '(+ 1 weiterer Standort)' from location."""
    loc = (location or "").strip()
    if not loc or loc == "nan":
        return ""
    loc = _LOCATION_MULTI_RE.sub("", loc).strip()
    # Collapse internal whitespace
    loc = re.sub(r"\s+", " ", loc)
    return loc


def _is_blocked_domain(url: str) -> bool:
    """Return True if URL host matches or is a subdomain of any BLOCKED_DOMAINS entry."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)
    except Exception:
        return False


# Controlled source vocabulary — maps raw source strings to canonical names
_SOURCE_MAP = {
    "indeed": "indeed",
    "linkedin": "linkedin",
    "glassdoor": "glassdoor",
    "google": "google",
    "jobspy": "jobspy",
    "jobroom": "jobroom",
    "serpapi": "serpapi",
    "jobs4sales": "jobs4sales",
    "ictjobs": "ictjobs",
    "greenhouse": "greenhouse",
    "lever": "lever",
    "jobsch": "jobsch",
}


def _normalize_source(raw_source: str) -> str:
    """Normalize source to controlled vocabulary. Preserves serpapi sub-source."""
    s = (raw_source or "unknown").strip().lower()
    # Handle "serpapi/via LinkedIn" etc.
    if s.startswith("serpapi"):
        return s  # keep sub-source info
    return _SOURCE_MAP.get(s, s)


def normalize_job(
    title: str,
    company: str,
    location: str,
    url: str,
    description: str,
    source: str,
    date_posted: str | None,
    salary: str | None,
    employment_type: str | None,
    search_term: str | None = None,
) -> dict | None:
    """Normalize a job listing to the standard schema.

    Validates and cleans all fields:
    - Title: whitespace collapsed, max 200 chars
    - Company: whitespace collapsed, no URLs
    - URL: tracking params stripped, must be http(s)
    - Source: mapped to controlled vocabulary
    - Description: max 5000 chars, not just title repeated

    Title-level filters (drops row, returns None):
    - Junior/clerical/operative/call-center/retail-sales (JUNIOR_TOKENS)
    - French/Italian (FR_IT_TOKENS)
    """
    # [M1] Collapse internal whitespace (newlines, tabs) to single spaces
    title = re.sub(r'\s+', ' ', (title or "")).strip()
    if not title or title == "nan":
        return None
    # Title max 200 chars
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0]

    # Title-level junk filter (junior/clerical, FR/IT). Cheap, runs before any I/O.
    drop_reason = _drop_by_title(title)
    if drop_reason:
        _bump_filter(drop_reason)
        log.debug(f"  Dropped ({drop_reason}): {title}")
        return None

    company = re.sub(r'\s+', ' ', (company or "")).strip()
    if company == "nan":
        company = ""
    # Company must not contain URLs (data quality issue)
    if company and re.search(r'https?://', company):
        company = ""

    desc_clean = (description or "").strip()
    if desc_clean == "nan":
        desc_clean = ""
    # Description must not be just the title repeated
    if desc_clean and desc_clean.strip().lower() == title.lower():
        desc_clean = ""

    # Clean URL (strip tracking params, validate format)
    clean = _clean_url(url)

    # Drop blocked aggregator/redirect-only domains before any downstream work
    if _is_blocked_domain(clean):
        log.info(f"  Skipped blocked domain for '{title}' @ '{company}': {clean}")
        return None

    # Try to extract company from description if missing
    if not company and desc_clean:
        company = _try_extract_company(desc_clean)
        if company:
            log.info(f"  Extracted company '{company}' from description for: {title}")

    # Fallback: extract company from URL domain (if not a job board)
    if not company and clean:
        try:
            from urllib.parse import urlparse
            domain = urlparse(clean).netloc.replace("www.", "").split('.')[0]
            _JOB_BOARD_STEMS = {
                "indeed", "linkedin", "glassdoor", "google", "jobs", "jobup",
                "jobcloud", "monster", "xing", "stepstone", "huzzle", "ch",
                "careers", "career", "apply", "hire", "work", "talent",
                "recruiting", "bewerbung",
            }
            if domain and domain.lower() not in _JOB_BOARD_STEMS and len(domain) >= 3:
                company = domain.capitalize()
                log.info(f"  Extracted company '{company}' from URL domain for: {title}")
        except Exception:
            pass

    # Log warnings for missing important fields
    if not company:
        log.warning(f"  Job '{title}' has no company name — cover letter/CV will be skipped")
    if not desc_clean:
        log.debug(f"  Job '{title}' at '{company}' has no description")

    return {
        "job_id": generate_job_id(title, company, clean),
        "title": title,
        "company": company,
        "location": _clean_location(location),
        "url": clean,
        "description": desc_clean[:5000],  # cap at 5000 chars
        "description_source": "original" if desc_clean else "empty",
        "source": _normalize_source(source),
        "date_posted": (date_posted or "").strip() if date_posted and str(date_posted) != "nan" else None,
        "salary": salary,
        "employment_type": (employment_type or "").strip() if employment_type and str(employment_type) != "nan" else None,
        "search_term": search_term,
    }


def scrape_serpapi(search_term: str, seen: dict[str, str], max_pages: int = DEFAULT_SERP_PAGES) -> list[dict]:
    """Scrape jobs using SERP API (Google Jobs engine).

    Features:
    - Round-robin key rotation across 2 API keys
    - Up to max_pages pages (10 results each)
    - Smart pagination: stops early if all results on a page are already seen
    """
    if not _serpapi_keys:
        log.warning("[serpapi] No API keys configured in .env")
        return []

    jobs = []
    next_page_token = None

    for page in range(max_pages):
        api_key = _get_serpapi_key()

        params = {
            "engine": "google_jobs",
            "q": search_term,
            "location": "Switzerland",
            "hl": "de",
            "gl": "ch",
            "api_key": api_key,
        }

        if next_page_token:
            params["next_page_token"] = next_page_token

        try:
            log.info(f"[serpapi] Searching: '{search_term}' page {page + 1}/{max_pages}")
            resp = requests.get(SERPAPI_URL, params=params, timeout=30)

            if resp.status_code == 429:
                log.warning(f"[serpapi] Rate limited on page {page + 1} for '{search_term}'")
                break

            resp.raise_for_status()

            try:
                data = resp.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                log.warning(f"[serpapi] Invalid JSON response for '{search_term}' page {page + 1}")
                break

            # Check for API errors
            if "error" in data:
                log.warning(f"[serpapi] API error for '{search_term}': {data['error']}")
                break

            results = data.get("jobs_results", [])
            if not results:
                log.info(f"[serpapi] No results on page {page + 1} for '{search_term}'")
                break

            page_jobs = []
            for item in results:
                job = _parse_serpapi_item(item, search_term=search_term)
                if job:
                    page_jobs.append(job)

            jobs.extend(page_jobs)

            # Smart pagination: if all results on this page are already seen, stop
            if seen and page_jobs:
                new_on_page = sum(1 for j in page_jobs if _job_hash(j) not in seen)
                if new_on_page == 0:
                    log.info(f"[serpapi] All {len(page_jobs)} results on page {page + 1} already seen, stopping pagination")
                    break
                log.info(f"[serpapi] {new_on_page}/{len(page_jobs)} new results on page {page + 1}")

            # Check for next page token
            pagination = data.get("serpapi_pagination", {})
            next_page_token = pagination.get("next_page_token")
            if not next_page_token:
                break

        except requests.exceptions.RequestException as e:
            log.warning(f"[serpapi] Network error for '{search_term}' page {page + 1}: {e}")
            break
        except Exception as e:
            log.warning(f"[serpapi] Error for '{search_term}' page {page + 1}: {e}")
            break

    if jobs:
        log.info(f"[serpapi] Found {len(jobs)} jobs for '{search_term}'")
    return jobs


def _parse_relative_date(text: str | None) -> str | None:
    """Convert SerpAPI relative date strings ('2 days ago', '1 week ago') to ISO dates.

    Returns ISO date string (YYYY-MM-DD) or None if unparseable.
    """
    if not text or not isinstance(text, str):
        return None
    text = text.strip().lower()

    # Already an ISO date? Return as-is
    if re.match(r'^\d{4}-\d{2}-\d{2}', text):
        return text[:10]

    # Parse relative patterns
    from datetime import timedelta, timezone
    now = datetime.now(timezone.utc)
    m = re.match(r'(\d+)\s+(hour|day|week|month|year)s?\s+ago', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "hour":
            return (now - timedelta(hours=n)).strftime("%Y-%m-%d")
        elif unit == "day":
            return (now - timedelta(days=n)).strftime("%Y-%m-%d")
        elif unit == "week":
            return (now - timedelta(weeks=n)).strftime("%Y-%m-%d")
        elif unit == "month":
            return (now - timedelta(days=n * 30)).strftime("%Y-%m-%d")
        elif unit == "year":
            return (now - timedelta(days=n * 365)).strftime("%Y-%m-%d")

    # Handle German relative dates ("vor 2 Tagen", "vor 1 Woche")
    m = re.match(r'vor\s+(\d+)\s+(stunde|tag|woche|monat|jahr)(?:en|n)?', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        deltas = {"stunde": timedelta(hours=n), "tag": timedelta(days=n),
                  "woche": timedelta(weeks=n), "monat": timedelta(days=n * 30),
                  "jahr": timedelta(days=n * 365)}
        delta = deltas.get(unit)
        if delta:
            return (now - delta).strftime("%Y-%m-%d")

    return None  # Unparseable date — return None instead of raw text


def _parse_serpapi_item(item: dict, search_term: str | None = None) -> dict | None:
    """Parse a SERP API Google Jobs result into normalized format."""
    title = item.get("title", "")
    company = item.get("company_name", "")
    location = item.get("location", "")

    # Best application URL — prefer direct apply links
    url = ""
    apply_options = item.get("apply_options", [])
    if apply_options:
        # Sort: direct links first, then by order
        direct = [opt for opt in apply_options if opt.get("is_direct")]
        url = (direct[0] if direct else apply_options[0]).get("link", "")

    description = item.get("description", "")

    # Extract structured metadata
    detected = item.get("detected_extensions", {})
    date_posted = _parse_relative_date(detected.get("posted_at"))
    salary = detected.get("salary")
    schedule = detected.get("schedule_type")

    # "via" tells us where Google found the listing (LinkedIn, Indeed, etc.)
    via = item.get("via", "")
    source = f"serpapi/{via.replace('via ', '')}" if via else "serpapi"

    return normalize_job(
        title=title,
        company=company,
        location=location,
        url=url,
        description=description,
        source=source.lower(),
        date_posted=date_posted,
        salary=salary,
        employment_type=schedule,
        search_term=search_term,
    )


def scrape_single_term(
    search_term: str,
    hours_old: int = DEFAULT_HOURS_OLD,
    seen: dict[str, str] | None = None,
    serp_pages: int = DEFAULT_SERP_PAGES,
) -> list[dict]:
    """
    Scrape JobSpy + SERP API for a single search term.
    Designed to run in parallel via ThreadPoolExecutor.
    """
    jobs = []

    # Scrape from JobSpy (Indeed, LinkedIn, Glassdoor, Google)
    jobs.extend(scrape_jobspy(search_term, hours_old=hours_old))

    # Scrape from SERP API (Google Jobs) with smart pagination
    if serp_pages > 0:
        jobs.extend(scrape_serpapi(search_term, seen or {}, max_pages=serp_pages))

    # Niche Swiss boards (jobchannel network). Each function internally skips
    # terms outside its sales/IT wheelhouse via _niche_term_is_relevant().
    jobs.extend(scrape_jobs4sales(search_term))
    jobs.extend(scrape_ictjobs(search_term))

    # job-room.ch is scraped once for all terms in main() (shared Playwright instance).

    log.info(f"[{search_term}] Collected {len(jobs)} jobs total")
    return jobs


def _normalize_title(title: str) -> str:
    """Normalize job title for fuzzy deduplication.

    Strips gender markers, percentage ranges, and common noise so that
    'CRM Specialist (m/f/d) 80-100%' and 'CRM Specialist' match.
    """
    t = title.lower().strip()
    # Remove gender markers: (m/f/d), (w/m/d), (m/w/d), (f/m/x), (all genders), (alle geschlechter), (a)
    t = re.sub(r"\s*\([-mwfdx/\s]+\)\s*", " ", t)
    t = re.sub(r"\s*\(all\s*genders?\)\s*", " ", t)
    t = re.sub(r"\s*\(alle\s*geschlechter\)\s*", " ", t)
    t = re.sub(r"\s*\(a\)\s*", " ", t)
    # Remove percentage ranges: 80-100%, 60-80%, 100%
    t = re.sub(r"\s*\d{2,3}\s*[-–]\s*\d{2,3}\s*%\s*", " ", t)
    t = re.sub(r"\s*\d{2,3}\s*%\s*", " ", t)
    # Remove asterisks, hash marks
    t = re.sub(r"[*#]", "", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _normalize_company(company: str) -> str:
    """Normalize company name for fuzzy deduplication.

    Strips legal suffixes (AG, GmbH, SA...) and geographic qualifiers
    so that 'Swisscom AG' and 'Swisscom (Schweiz) AG' match.
    Preserves division/department qualifiers to avoid merging different entities.
    """
    c = company.lower().strip()
    # Only remove geographic/country parenthetical qualifiers (not divisions)
    c = re.sub(
        r"\s*\((schweiz|switzerland|ch|suisse|svizzera|europe|emea|deutschland|germany|österreich|austria)\)\s*",
        " ", c, flags=re.IGNORECASE,
    )
    # Remove legal suffixes (must be word-boundary)
    c = re.sub(r"\b(?:ag|gmbh|sa|sàrl|sarl|ltd|inc|se|co\.?\s*kg|&\s*co\.?|group|holding|plc)\b\.?", "", c)
    # Collapse whitespace
    c = re.sub(r"\s+", " ", c).strip()
    return c


def _normalize_location(location: str) -> str:
    """Normalize a job location string for dedup.

    Lowercases, strips country/canton suffixes and aggregator UI noise so
    that 'Zürich, ZH, Schweiz' and 'Zurich' match.
    """
    if not location:
        return ""
    loc = location.lower().strip()
    loc = re.sub(r"\(\+\s*\d+\s*\w+\s*standorte?\)", " ", loc)
    loc = re.sub(r"\b(schweiz|switzerland|suisse|svizzera|ch)\b\.?", " ", loc)
    loc = re.sub(r"\b(zh|be|bs|bl|lu|sg|gr|ti|vd|ge|fr|so|sh|tg|ag|ar|ai|gl|ow|nw|sz|ur|zg|ne|ju|vs)\b\.?", " ", loc)
    loc = loc.replace("ü", "u").replace("ä", "a").replace("ö", "o")
    loc = re.sub(r"[,;/]+", " ", loc)
    loc = re.sub(r"\s+", " ", loc).strip()
    return loc


def _fuzzy_dedup_key(job: dict) -> str:
    """Generate a normalized dedup key from title + company.

    Falls back to including URL when company is missing (common with
    Glassdoor) to prevent different jobs with the same title from
    collapsing into one.
    """
    norm_title = _normalize_title(job.get("title", ""))
    norm_company = _normalize_company(job.get("company", ""))

    # No company → include URL to prevent false dedup
    # (e.g. two different "CRM Specialist" jobs from Glassdoor with no company)
    if not norm_company:
        url = job.get("url", "").strip().rstrip("/").lower()
        return f"{norm_title}||{url}"

    return f"{norm_title}|{norm_company}"


def deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicate job listings using fuzzy title+company matching.

    Dedup layers (in order):
    1. Exact URL match (normalized: lowercase, strip trailing /)
    2. Fuzzy title+company match (normalized: strip gender markers, legal suffixes, etc.)

    Always keeps the listing with the longest description.
    """
    url_map: dict[str, int] = {}  # url -> index in unique
    fuzzy_map: dict[str, int] = {}  # normalized key -> index in unique
    unique = []

    for job in jobs:
        # URL already cleaned by normalize_job (tracking params stripped)
        url = job.get("url", "").strip().rstrip("/").lower()
        fuzzy_key = _fuzzy_dedup_key(job)
        desc_len = len(job.get("description", ""))

        # Check if duplicate by URL
        if url and url in url_map:
            existing_idx = url_map[url]
            if desc_len > len(unique[existing_idx].get("description", "")):
                unique[existing_idx] = job
            continue

        # Check if duplicate by fuzzy title+company
        if fuzzy_key in fuzzy_map:
            existing_idx = fuzzy_map[fuzzy_key]
            existing = unique[existing_idx]
            log.debug(
                f"  Fuzzy dedup: '{job['title']}' at '{job['company']}' ({job.get('source', '?')}) "
                f"≈ '{existing['title']}' at '{existing['company']}' ({existing.get('source', '?')})"
            )
            if desc_len > len(existing.get("description", "")):
                unique[existing_idx] = job
            continue

        idx = len(unique)
        if url:
            url_map[url] = idx
        fuzzy_map[fuzzy_key] = idx
        unique.append(job)

    return unique


# ---------------------------------------------------------------------------
# Phase 2: Fetch missing descriptions from actual job URLs
# ---------------------------------------------------------------------------

DESC_CACHE_FILE = TMP_DIR / "description_cache.json"
_FETCH_MIN_DESC_LEN = 50  # descriptions shorter than this are "empty"
_FETCH_MAX_DESC_LEN = 5000

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Per-domain rate limiting: track last request time per domain
_domain_last_request: dict[str, float] = {}
_domain_lock = threading.Lock()
_DOMAIN_MIN_DELAY = 2.0  # seconds between same-domain requests


def _load_desc_cache() -> dict:
    """Load description cache from disk."""
    if DESC_CACHE_FILE.exists():
        try:
            with open(DESC_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_desc_cache(cache: dict):
    """Save description cache to disk."""
    with open(DESC_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _get_domain(url: str) -> str:
    """Extract domain from URL."""
    from urllib.parse import urlparse
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _rate_limit_domain(domain: str):
    """Wait if needed to respect per-domain rate limits."""
    import random
    with _domain_lock:
        last = _domain_last_request.get(domain, 0)
        elapsed = time.time() - last
        if elapsed < _DOMAIN_MIN_DELAY:
            wait = _DOMAIN_MIN_DELAY - elapsed + random.uniform(0.5, 2.0)
            time.sleep(wait)
        _domain_last_request[domain] = time.time()


def _strip_html(html: str) -> str:
    """Strip HTML tags and extract text content.

    Removes script, style, nav, header, footer elements first,
    then strips remaining tags and normalizes whitespace.
    """
    if not html:
        return ""

    # Remove script, style, nav, header, footer blocks
    for tag in ("script", "style", "nav", "header", "footer", "noscript"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", html, flags=re.DOTALL | re.IGNORECASE)

    # Remove HTML comments
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)

    # Decode common HTML entities
    import html as html_mod
    html = html_mod.unescape(html)

    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def _detect_login_wall(text: str) -> bool:
    """Detect if fetched content is a login wall rather than job content."""
    if not text:
        return True
    first_500 = text[:500].lower()
    login_signals = [
        "sign in", "log in", "anmelden", "einloggen", "create account",
        "register to", "join now", "captcha", "access denied", "403 forbidden",
    ]
    matches = sum(1 for s in login_signals if s in first_500)
    return matches >= 2  # need at least 2 signals to reduce false positives


def _fetch_single_description(url: str, cache: dict) -> tuple[str, str, str]:
    """Fetch description for a single job URL.

    Returns (url, description, status) where status is:
    - "fetched": successfully retrieved
    - "blocked": login wall detected
    - "error": network/parsing error
    - "cached_blocked": previously blocked URL (skipped)
    """
    import random

    # Check cache — skip URLs marked blocked/error within 7 days
    if url in cache:
        cached = cache[url]
        fetched_at = cached.get("fetched_at", "")
        status = cached.get("status", "")
        if status == "fetched":
            return (url, cached.get("description", ""), "fetched")
        if status in ("blocked", "error") and fetched_at:
            try:
                cached_time = datetime.fromisoformat(fetched_at)
                if (datetime.now(timezone.utc) - cached_time).days < 7:
                    return (url, "", f"cached_{status}")
            except (ValueError, TypeError):
                pass

    domain = _get_domain(url)

    # Skip LinkedIn (always login wall)
    if "linkedin.com" in domain:
        return (url, "", "blocked")

    _rate_limit_domain(domain)

    try:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)

        if resp.status_code != 200:
            return (url, "", "error")

        text = _strip_html(resp.text)

        # Check for login wall
        if _detect_login_wall(text):
            return (url, "", "blocked")

        # Glassdoor-specific: if we got very little content, it's likely blocked
        if "glassdoor" in domain and len(text) < 100:
            return (url, "", "blocked")

        # Cap length
        text = text[:_FETCH_MAX_DESC_LEN]

        if len(text) < _FETCH_MIN_DESC_LEN:
            return (url, "", "blocked")

        return (url, text, "fetched")

    except requests.exceptions.Timeout:
        return (url, "", "error")
    except requests.exceptions.RequestException:
        return (url, "", "error")
    except Exception as e:
        log.debug(f"  Unexpected error fetching {url}: {e}")
        return (url, "", "error")


def fetch_missing_descriptions(jobs: list[dict]) -> list[dict]:
    """Fetch descriptions for jobs with empty/short descriptions.

    Visits actual job URLs with rate limiting and caching.
    Updates jobs in-place and returns the modified list.
    """
    cache = _load_desc_cache()

    # Find jobs needing descriptions
    to_fetch = []
    for job in jobs:
        desc = job.get("description", "").strip()
        url = job.get("url", "").strip()
        if len(desc) < _FETCH_MIN_DESC_LEN and url and url.startswith("http"):
            to_fetch.append(job)

    if not to_fetch:
        log.info("[fetch-desc] No jobs with missing descriptions")
        return jobs

    log.info(f"[fetch-desc] Fetching descriptions for {len(to_fetch)} jobs...")

    # Build URL -> job index mapping
    url_to_jobs: dict[str, list[dict]] = {}
    for job in to_fetch:
        url = job["url"].strip()
        url_to_jobs.setdefault(url, []).append(job)

    unique_urls = list(url_to_jobs.keys())
    fetched_count = 0
    blocked_count = 0
    error_count = 0
    cached_count = 0

    # Fetch in parallel with limited workers
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_fetch_single_description, url, cache): url
            for url in unique_urls
        }

        for future in as_completed(futures):
            url = futures[future]
            try:
                _, description, status = future.result()
            except Exception as e:
                log.debug(f"  Fetch failed for {url}: {e}")
                description, status = "", "error"

            # Update cache
            if status not in ("cached_blocked", "cached_error"):
                cache[url] = {
                    "description": description,
                    "status": status,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }

            # Update matching jobs
            if status == "fetched" and description:
                for job in url_to_jobs.get(url, []):
                    job["description"] = description
                    job["description_source"] = "fetched"
                    fetched_count += 1
            elif "cached" in status:
                cached_count += 1
            elif status == "blocked":
                blocked_count += 1
            else:
                error_count += 1

    _save_desc_cache(cache)
    log.info(
        f"[fetch-desc] Results: {fetched_count} fetched, "
        f"{blocked_count} blocked, {error_count} errors, {cached_count} cached-skip"
    )

    return jobs


def _job_hash(job: dict) -> str:
    """Generate a stable hash for a job based on normalized title + company.

    URL is intentionally excluded so the same job posted on Indeed and
    LinkedIn (different URLs) produces the same hash. Normalization strips
    gender markers, legal suffixes, etc.
    """
    raw = _fuzzy_dedup_key(job)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def load_seen_jobs() -> dict[str, dict]:
    """Load previously seen job hashes from disk.

    Returns dict: hash -> {"first_seen": ISO ts, "source": str|None}.
    Migrates legacy entries (hash -> ISO string) on read.
    """
    if not SEEN_JOBS_FILE.exists():
        return {}
    with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for h, v in raw.items():
        if isinstance(v, str):
            out[h] = {"first_seen": v, "source": None}
        elif isinstance(v, dict):
            out[h] = {"first_seen": v.get("first_seen", ""), "source": v.get("source")}
    return out


def save_seen_jobs(seen: dict[str, dict]):
    """Persist seen job hashes to disk."""
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def _job_hash_legacy(job: dict) -> str:
    """Legacy hash format including URL (for backward compatibility with old seen_jobs.json)."""
    raw = f"{job.get('title', '').lower().strip()}|{job.get('company', '').lower().strip()}|{job.get('url', '').strip().rstrip('/').lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _job_content_hash(job: dict) -> str:
    """Content hash including normalized location.

    Catches re-posts where title/company normalize identically and the
    location matches, even when the URL has changed (tracking params,
    new aggregator). Stored alongside _job_hash so seen-lookup matches
    on either axis.
    """
    norm_title = _normalize_title(job.get("title", ""))
    norm_company = _normalize_company(job.get("company", ""))
    norm_loc = _normalize_location(job.get("location", ""))
    raw = f"{norm_title}|{norm_company}|{norm_loc}"
    return "c:" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def filter_new_jobs(jobs: list[dict], seen: dict[str, str]) -> list[dict]:
    """Filter out jobs we've already seen in previous runs.

    Checks new (normalized, no URL), content (+ location), and legacy
    (with URL) hashes for backward compatibility with existing
    seen_jobs.json entries.
    """
    new_jobs = []
    for job in jobs:
        h_new = _job_hash(job)
        h_legacy = _job_hash_legacy(job)
        h_content = _job_content_hash(job)
        if h_new not in seen and h_legacy not in seen and h_content not in seen:
            new_jobs.append(job)
    return new_jobs


def main():
    """Run the full scraping pipeline with parallel execution."""
    parser = argparse.ArgumentParser(description="Scrape Swiss job listings")
    parser.add_argument("--hours-old", type=int, default=None,
                        help=f"How far back to search in hours (default: {DEFAULT_HOURS_OLD} = 7 days; --deep overrides to {DEEP_HOURS_OLD})")
    parser.add_argument("--deep", action="store_true",
                        help=f"Backfill mode: scrape last {DEEP_HOURS_OLD}h (~30 days) instead of the default {DEFAULT_HOURS_OLD}h")
    parser.add_argument("--reset-seen", action="store_true",
                        help="Clear the seen jobs history and start fresh")
    parser.add_argument("--serp-pages", type=int, default=DEFAULT_SERP_PAGES,
                        help=f"Max SERP API pages per search term (default: {DEFAULT_SERP_PAGES}, 10 results/page)")
    parser.add_argument("--no-serp", action="store_true",
                        help="Skip SERP API entirely (only use python-jobspy)")
    parser.add_argument("--no-fetch-descriptions", action="store_true",
                        help="Skip fetching missing descriptions from job URLs")
    parser.add_argument("--no-jobroom", action="store_true",
                        help="Skip job-room.ch (Swiss federal RAV/SECO board).")
    parser.add_argument("--no-jobsch", action="store_true",
                        help="Skip jobs.ch (largest CH job board, public search API).")
    parser.add_argument("--no-ats", action="store_true",
                        help="Skip public ATS boards (Greenhouse + Lever curated CH employers).")
    args = parser.parse_args()

    if args.hours_old is None:
        args.hours_old = DEEP_HOURS_OLD if args.deep else DEFAULT_HOURS_OLD
    elif args.deep:
        log.warning(f"Both --hours-old={args.hours_old} and --deep given; using explicit --hours-old")

    TMP_DIR.mkdir(parents=True, exist_ok=True)

    # Load or reset seen jobs history
    if args.reset_seen:
        seen = {}
        log.info("Seen jobs history cleared (--reset-seen)")
    else:
        seen = load_seen_jobs()
        log.info(f"Loaded {len(seen)} previously seen jobs from history")

    serp_pages = 0 if args.no_serp else args.serp_pages

    # Log source configuration
    log.info(f"Sources: python-jobspy (4 sites)")
    if serp_pages > 0 and _serpapi_keys:
        log.info(f"Sources: SERP API ({len(_serpapi_keys)} key(s), up to {serp_pages} pages/term)")
    elif serp_pages > 0:
        log.warning("SERP API requested but no API keys in .env — skipping")
        serp_pages = 0

    log.info(f"Starting parallel scraping for {len(SEARCH_TERMS)} search terms (last {args.hours_old}h)...")
    start_time = time.time()

    all_jobs = []

    # Snapshot seen dict for thread-safe read access during parallel scraping
    seen_snapshot = dict(seen)

    # Parallel execution: scrape all search terms concurrently
    with ThreadPoolExecutor(max_workers=len(SEARCH_TERMS)) as executor:
        future_to_term = {
            executor.submit(scrape_single_term, term, args.hours_old, seen_snapshot, serp_pages): term
            for term in SEARCH_TERMS
        }

        for future in as_completed(future_to_term):
            term = future_to_term[future]
            try:
                jobs = future.result()
                all_jobs.extend(jobs)
            except Exception as e:
                log.error(f"[{term}] Failed with exception: {e}")

    elapsed = time.time() - start_time
    log.info(f"Parallel scraping completed in {elapsed:.1f}s")
    log.info(f"Total raw jobs collected: {len(all_jobs)}")

    # job-room.ch — scraped once across all terms (direct API, sequential).
    if not args.no_jobroom:
        jr_start = time.time()
        log.info(f"[jobroom] Scraping {len(SEARCH_TERMS)} terms...")
        jr_jobs = scrape_jobroom_all(SEARCH_TERMS, hours_old=args.hours_old)
        all_jobs.extend(jr_jobs)
        log.info(f"[jobroom] Collected {len(jr_jobs)} jobs in {time.time() - jr_start:.1f}s")

    # jobs.ch — largest CH board, public search API (sequential).
    if not args.no_jobsch:
        js_start = time.time()
        log.info(f"[jobsch] Scraping {len(SEARCH_TERMS)} terms...")
        js_jobs = scrape_jobsch_all(SEARCH_TERMS, hours_old=args.hours_old)
        all_jobs.extend(js_jobs)
        log.info(f"[jobsch] Collected {len(js_jobs)} jobs in {time.time() - js_start:.1f}s")

    # Public ATS boards (Greenhouse / Lever) — curated CH employer list.
    if not args.no_ats:
        ats_start = time.time()
        log.info("[ats] Scraping public ATS boards (Greenhouse + Lever)...")
        ats_jobs = scrape_ats_boards_all()
        all_jobs.extend(ats_jobs)
        log.info(f"[ats] Collected {len(ats_jobs)} CH jobs in {time.time() - ats_start:.1f}s")

    unique_jobs = deduplicate(all_jobs)
    log.info(f"After deduplication: {len(unique_jobs)}")

    # Fetch missing descriptions from actual job URLs
    if not args.no_fetch_descriptions:
        unique_jobs = fetch_missing_descriptions(unique_jobs)

    # Filter out previously seen jobs
    new_jobs = filter_new_jobs(unique_jobs, seen)
    skipped = len(unique_jobs) - len(new_jobs)
    if skipped > 0:
        log.info(f"Skipped {skipped} previously seen jobs")
    log.info(f"New jobs to process: {len(new_jobs)}")

    # Add scrape timestamp
    scraped_at = datetime.now(timezone.utc).isoformat()
    for job in new_jobs:
        job["scraped_at"] = scraped_at

    # Update seen jobs history with all unique jobs (including skipped ones).
    # Store both the title+company hash and the title+company+location content
    # hash so future runs can detect re-posts on either axis.
    for job in unique_jobs:
        src = job.get("source") or None
        for h in (_job_hash(job), _job_content_hash(job)):
            if h not in seen:
                seen[h] = {"first_seen": scraped_at, "source": src}
    save_seen_jobs(seen)
    log.info(f"Updated seen jobs history: {len(seen)} total entries")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(new_jobs, f, ensure_ascii=False, indent=2)

    log.info(f"Saved {len(new_jobs)} new jobs to {OUTPUT_FILE}")
    log.info(f"Total pipeline time: {elapsed:.1f}s (avg {elapsed/len(SEARCH_TERMS):.1f}s per term)")

    # Log SERP API budget usage
    if _serpapi_call_count > 0:
        log.info(f"SERP API calls this run: {_serpapi_call_count} (budget: 500/month across 2 keys)")

    # Title-filter drop totals (junior/clerical, FR/IT)
    if _filter_stats.get("junior") or _filter_stats.get("fr_it"):
        log.info(
            f"[filter] dropped {_filter_stats.get('junior', 0)} junior/clerical, "
            f"{_filter_stats.get('fr_it', 0)} FR/IT titles"
        )

    # Niche-board health check: relevant terms ran but 0 jobs returned → probably
    # a site-side markup change. Escalate to error so the user notices quickly.
    for source, stats in _niche_stats.items():
        if stats["relevant_terms"] > 0 and stats["jobs"] == 0:
            log.error(
                f"[{source}] 0 jobs across {stats['relevant_terms']} relevant terms — "
                "likely markup change or site outage; inspect parser"
            )
        elif stats["relevant_terms"] > 0:
            log.info(
                f"[{source}] {stats['jobs']} jobs across {stats['relevant_terms']} relevant terms"
            )

    return new_jobs


if __name__ == "__main__":
    main()
