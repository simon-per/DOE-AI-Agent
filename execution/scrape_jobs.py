"""
Stage 1: Scrape Swiss job listings from multiple sources.
Output: .tmp/raw_jobs.json

Sources:
- python-jobspy (Indeed, LinkedIn, Glassdoor, Google)
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
            "CRM Specialist", "Dynamics 365", "Digital Sales Specialist",
            "Sales Operations",
        ]

SEARCH_TERMS = _get_search_terms()

LOCATION = "Switzerland"
DEFAULT_HOURS_OLD = 720  # 30 days for initial/broad fetch

# --- SERP API configuration ---
SERPAPI_URL = "https://serpapi.com/search"
DEFAULT_SERP_PAGES = 3  # up to 30 results per term (10/page)

# Round-robin key rotation for 2× budget (500 calls/month)
_serpapi_keys: list[str] = []
for _key_name in ("SERPAPI_API_KEY", "SERPAPI_API_KEY2"):
    _k = os.getenv(_key_name, "").strip()
    if _k:
        _serpapi_keys.append(_k)

_serpapi_lock = threading.Lock()
_serpapi_call_count = 0


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
    """Scrape jobs using python-jobspy (Indeed, LinkedIn, Glassdoor, Google)."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("python-jobspy not installed. Run: pip install python-jobspy")
        return []

    jobs = []
    try:
        log.info(f"[jobspy] Searching: '{search_term}' in {LOCATION} (last {hours_old}h)")
        results = scrape_jobs(
            site_name=["indeed", "linkedin", "glassdoor", "google"],
            search_term=search_term,
            location=LOCATION,
            results_wanted=25,
            hours_old=hours_old,
            country_indeed="Switzerland",
            linkedin_fetch_description=True,
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


def scrape_jobroom(search_term: str) -> list[dict]:
    """Scrape jobs from job-room.ch frontend search."""
    jobs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        "Referer": "https://www.job-room.ch/",
        "Origin": "https://www.job-room.ch",
    }

    search_url = "https://ob.job-room.ch/api/jobAdvertisements/_search"
    payload = {
        "page": 0,
        "size": 50,
        "body": {
            "keywordsText": search_term,
            "onlineSinceDays": 7,
        },
    }

    for attempt in range(3):
        try:
            log.info(f"[jobroom] Searching: '{search_term}' (attempt {attempt + 1})")
            resp = requests.post(search_url, json=payload, headers=headers, timeout=30)

            if resp.status_code == 200:
                data = resp.json()
                results = data.get("result", data.get("content", []))
                if isinstance(results, list):
                    for item in results:
                        job = _parse_jobroom_item(item)
                        if job:
                            jobs.append(job)
                log.info(f"[jobroom] Found {len(jobs)} jobs for '{search_term}'")
                break  # success
            else:
                log.warning(f"[jobroom] HTTP {resp.status_code} for '{search_term}'")
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
        except Exception as e:
            log.warning(f"[jobroom] Error searching '{search_term}': {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue

    return jobs


def _parse_jobroom_item(item: dict) -> dict | None:
    """Parse a job-room.ch search result item."""
    try:
        title = item.get("title", "") or item.get("jobTitle", "")
        company = item.get("company", {}).get("name", "") if isinstance(item.get("company"), dict) else str(item.get("company", ""))
        location_data = item.get("location", {})
        if isinstance(location_data, dict):
            city = location_data.get("city", "")
            canton = location_data.get("cantonCode", "")
            location = f"{city}, {canton}" if city and canton else (city or canton)
        else:
            location = str(location_data)

        job_id = item.get("id", "")
        url = f"https://www.job-room.ch/job-search/{job_id}" if job_id else ""
        description = item.get("description", "") or item.get("jobDescription", "")
        date_posted = item.get("publicationDate", None)
        employment_type = item.get("workload", None)

        return normalize_job(
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            source="jobroom",
            date_posted=date_posted,
            salary=None,
            employment_type=str(employment_type) if employment_type else None,
        )
    except Exception as e:
        log.warning(f"[jobroom] Failed to parse item: {e}")
        return None


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


# Controlled source vocabulary — maps raw source strings to canonical names
_SOURCE_MAP = {
    "indeed": "indeed",
    "linkedin": "linkedin",
    "glassdoor": "glassdoor",
    "google": "google",
    "jobspy": "jobspy",
    "jobroom": "jobroom",
    "serpapi": "serpapi",
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
) -> dict | None:
    """Normalize a job listing to the standard schema.

    Validates and cleans all fields:
    - Title: whitespace collapsed, max 200 chars
    - Company: whitespace collapsed, no URLs
    - URL: tracking params stripped, must be http(s)
    - Source: mapped to controlled vocabulary
    - Description: max 5000 chars, not just title repeated
    """
    # [M1] Collapse internal whitespace (newlines, tabs) to single spaces
    title = re.sub(r'\s+', ' ', (title or "")).strip()
    if not title or title == "nan":
        return None
    # Title max 200 chars
    if len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0]

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
        "location": "" if (location or "").strip() == "nan" else (location or "").strip(),
        "url": clean,
        "description": desc_clean[:5000],  # cap at 5000 chars
        "description_source": "original" if desc_clean else "empty",
        "source": _normalize_source(source),
        "date_posted": (date_posted or "").strip() if date_posted and str(date_posted) != "nan" else None,
        "salary": salary,
        "employment_type": (employment_type or "").strip() if employment_type and str(employment_type) != "nan" else None,
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
                job = _parse_serpapi_item(item)
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


def _parse_serpapi_item(item: dict) -> dict | None:
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

    # Job-Room disabled: SSL cert only valid for www.job-room.ch (not ob.job-room.ch).
    # The www API returns empty 200s — Angular SPA requires browser-side CSRF tokens.
    # Would need Playwright headless browser to scrape. Google Jobs already aggregates
    # Job-Room listings via SerpAPI, so coverage gap is minimal.
    # jobs.extend(scrape_jobroom(search_term))

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


def load_seen_jobs() -> dict[str, str]:
    """Load previously seen job hashes from disk.

    Returns dict: hash -> first_seen ISO timestamp.
    """
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen_jobs(seen: dict[str, str]):
    """Persist seen job hashes to disk."""
    with open(SEEN_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def _job_hash_legacy(job: dict) -> str:
    """Legacy hash format including URL (for backward compatibility with old seen_jobs.json)."""
    raw = f"{job.get('title', '').lower().strip()}|{job.get('company', '').lower().strip()}|{job.get('url', '').strip().rstrip('/').lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def filter_new_jobs(jobs: list[dict], seen: dict[str, str]) -> list[dict]:
    """Filter out jobs we've already seen in previous runs.

    Checks both new (normalized, no URL) and legacy (with URL) hashes
    for backward compatibility with existing seen_jobs.json entries.
    """
    new_jobs = []
    for job in jobs:
        h_new = _job_hash(job)
        h_legacy = _job_hash_legacy(job)
        if h_new not in seen and h_legacy not in seen:
            new_jobs.append(job)
    return new_jobs


def main():
    """Run the full scraping pipeline with parallel execution."""
    parser = argparse.ArgumentParser(description="Scrape Swiss job listings")
    parser.add_argument("--hours-old", type=int, default=DEFAULT_HOURS_OLD,
                        help=f"How far back to search in hours (default: {DEFAULT_HOURS_OLD} = ~30 days)")
    parser.add_argument("--reset-seen", action="store_true",
                        help="Clear the seen jobs history and start fresh")
    parser.add_argument("--serp-pages", type=int, default=DEFAULT_SERP_PAGES,
                        help=f"Max SERP API pages per search term (default: {DEFAULT_SERP_PAGES}, 10 results/page)")
    parser.add_argument("--no-serp", action="store_true",
                        help="Skip SERP API entirely (only use python-jobspy)")
    parser.add_argument("--no-fetch-descriptions", action="store_true",
                        help="Skip fetching missing descriptions from job URLs")
    args = parser.parse_args()

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

    # Update seen jobs history with all unique jobs (including skipped ones)
    for job in unique_jobs:
        h = _job_hash(job)
        if h not in seen:
            seen[h] = scraped_at
    save_seen_jobs(seen)
    log.info(f"Updated seen jobs history: {len(seen)} total entries")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(new_jobs, f, ensure_ascii=False, indent=2)

    log.info(f"Saved {len(new_jobs)} new jobs to {OUTPUT_FILE}")
    log.info(f"Total pipeline time: {elapsed:.1f}s (avg {elapsed/len(SEARCH_TERMS):.1f}s per term)")

    # Log SERP API budget usage
    if _serpapi_call_count > 0:
        log.info(f"SERP API calls this run: {_serpapi_call_count} (budget: 500/month across 2 keys)")

    return new_jobs


if __name__ == "__main__":
    main()
