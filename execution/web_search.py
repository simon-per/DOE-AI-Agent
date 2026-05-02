"""
Reusable web search module for company research via SerpAPI Google Search.

Provides Google Search capabilities using existing SerpAPI keys (same as scrape_jobs.py).
Primary use: find company websites for cover letter personalization instead of guessing URLs.

Functions:
    search_google()          — SerpAPI Google Search wrapper
    search_company_website() — Find a company's official website
    fetch_page_text()        — Fetch and extract clean text from a webpage
    research_company()       — Full company research pipeline with caching

Budget: Each search_google() call costs 1 SERP credit (shared with job scraping).
        500 calls/month total (2 keys x 250). Cache aggressively.

Usage:
    from execution.web_search import research_company
    result = research_company("KOWI Assurance AG", location="Switzerland",
                              job_description="...", job_url="...")
    # result = {
    #     "description": "KOWI Assurance AG is a Swiss insurance brokerage...",
    #     "website": "https://www.kowi-assurance.ch",
    #     "source": "google_search",
    #     "confidence": 0.9
    # }
"""

import html as html_module
import json
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
COMPANY_CACHE_FILE = TMP_DIR / "company_cache.json"

SERPAPI_URL = "https://serpapi.com/search"

# Round-robin key rotation (same keys as scrape_jobs.py)
_serpapi_keys: list[str] = []
for _key_name in ("SERPAPI_API_KEY", "SERPAPI_API_KEY2"):
    _k = os.getenv(_key_name, "").strip()
    if _k:
        _serpapi_keys.append(_k)

_serpapi_lock = threading.Lock()
_serpapi_call_count = 0

# Domains to exclude from company website results
JOB_BOARD_DOMAINS = {
    "indeed.com", "indeed.ch", "linkedin.com", "glassdoor.com", "glassdoor.ch",
    "jobs.ch", "jobup.ch", "jobcloud.ch", "google.com", "monster.ch",
    "xing.com", "stepstone.ch", "karriere.at", "huzzle.com",
    "wikipedia.org", "wikidata.org", "facebook.com", "twitter.com",
    "instagram.com", "youtube.com", "tiktok.com", "kununu.com",
    "comparis.ch", "handelsregister.help", "zefix.ch", "moneyhouse.ch",
    "northdata.com", "dnb.com", "crunchbase.com", "bloomberg.com",
    "jobagent.ch", "job.ch", "jobscout24.ch", "ostjob.ch", "topjobs.ch",
    "berufsberatung.ch", "arbeitgeber.ch",
    "simplyhired.com", "simplyhired.ch", "simplyhired.de",
}

# User-Agent rotation to reduce blocks
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]


# ---------------------------------------------------------------------------
# SerpAPI key management
# ---------------------------------------------------------------------------

def _get_serpapi_key() -> str | None:
    """Get next SERP API key using round-robin rotation."""
    global _serpapi_call_count
    if not _serpapi_keys:
        return None
    with _serpapi_lock:
        key = _serpapi_keys[_serpapi_call_count % len(_serpapi_keys)]
        _serpapi_call_count += 1
    return key


# ---------------------------------------------------------------------------
# Core search function
# ---------------------------------------------------------------------------

def search_google(query: str, num_results: int = 3) -> list[dict]:
    """Search Google via SerpAPI. Returns [{title, link, snippet}, ...].

    Each call costs 1 SERP credit (shared budget with job scraping).
    Returns empty list if no API keys available or search fails.
    """
    api_key = _get_serpapi_key()
    if not api_key:
        log.warning("[web_search] No SerpAPI keys available")
        return []

    params = {
        "engine": "google",
        "q": query,
        "num": num_results,
        "gl": "ch",  # Google country: Switzerland
        "hl": "de",  # Interface language: German
        "api_key": api_key,
    }

    for attempt in range(3):
        try:
            resp = requests.get(SERPAPI_URL, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 2 ** attempt * 2 + random.uniform(0, 2)
                log.warning(f"[web_search] Rate limited, waiting {wait:.1f}s (attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("organic_results", []):
                results.append({
                    "title": item.get("title", ""),
                    "link": item.get("link", ""),
                    "snippet": item.get("snippet", ""),
                })
            return results[:num_results]

        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if attempt < 2 and status >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            log.warning(f"[web_search] SerpAPI error (HTTP {status}): {e}")
            return []
        except Exception as e:
            log.warning(f"[web_search] Search failed: {e}")
            return []

    return []


# ---------------------------------------------------------------------------
# Company website finder
# ---------------------------------------------------------------------------

def _is_job_board(url: str) -> bool:
    """Check if a URL belongs to a known job board or non-company site."""
    try:
        domain = urlparse(url).netloc.replace("www.", "").lower()
        return any(board in domain for board in JOB_BOARD_DOMAINS)
    except Exception:
        return False


def search_company_website(company_name: str, location: str = "Switzerland",
                           job_title: str = "") -> str | None:
    """Find a company's official website via Google Search.

    Uses job title to add industry context when available, improving results
    for companies with generic names (e.g., 'KOWI Assurance AG insurance').
    Filters out job boards, social media, directories, etc.
    Returns the best URL or None.

    Costs 1 SERP credit.
    """
    if not company_name or len(company_name.strip()) < 2:
        return None

    # Build query with industry context from job title if available
    if job_title:
        # Extract potential industry keywords from title (skip very common words)
        skip_words = {"specialist", "manager", "analyst", "consultant", "director",
                      "lead", "senior", "junior", "head", "intern", "trainee",
                      "leiter", "leiterin", "berater", "beraterin", "mitarbeiter",
                      "mitarbeiterin", "sachbearbeiter", "sachbearbeiterin",
                      "m", "w", "d", "f", "mwd", "100", "80", "60"}
        title_words = [w for w in re.split(r'[\s/|()%–-]+', job_title)
                       if len(w) > 2 and w.lower() not in skip_words]
        # Use up to 2 industry-relevant words from the title
        context = " ".join(title_words[:2]) if title_words else ""
        query = f"{company_name} {context} {location}".strip()
    else:
        query = f"{company_name} official website {location}"

    results = search_google(query, num_results=5)

    if not results:
        log.info(f"  No Google Search results for: {query}")
        return None

    # Filter and rank results
    for result in results:
        url = result.get("link", "")
        if not url:
            continue
        if _is_job_board(url):
            continue

        # Prefer results where company name appears in title or snippet
        title_lower = result.get("title", "").lower()
        snippet_lower = result.get("snippet", "").lower()

        # Strip legal suffixes for matching
        clean_name = strip_legal_suffixes(company_name)
        clean_lower = clean_name.lower()

        # Check if company name (or significant part) is in the result
        if clean_lower and (clean_lower in title_lower or clean_lower in snippet_lower):
            log.info(f"  Company website (google_search): {url}")
            return url

        # Fuzzy: check if key words from company name appear (word-boundary match)
        key_words = [w.lower() for w in clean_name.split() if len(w) > 2]
        if key_words:
            title_matches = sum(1 for w in key_words if re.search(rf'\b{re.escape(w)}\b', title_lower))
            if title_matches >= max(1, int(len(key_words) * 0.75)):
                log.info(f"  Company website (google_search, fuzzy): {url}")
                return url

    # No result matched the company name — return None instead of guessing
    log.info(f"  Google Search found results but none matched '{company_name}'")
    return None


# ---------------------------------------------------------------------------
# Webpage text extraction
# ---------------------------------------------------------------------------

def fetch_page_text(url: str, max_chars: int = 5000) -> str | None:
    """Fetch a webpage and extract clean text content.

    Enhanced version with:
    - Better HTML-to-text conversion (strips nav, footer, sidebar)
    - Longer content limit (5000 chars) for richer context
    - User-Agent rotation to avoid blocks
    """
    try:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        }
        resp = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
        if resp.status_code != 200:
            return None

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type:
            return None

        text = resp.text

        # Remove non-content blocks
        for tag in ["script", "style", "nav", "footer", "header", "aside", "noscript"]:
            text = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', text, flags=re.DOTALL | re.IGNORECASE)

        # Remove HTML comments
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

        # Strip HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)

        # Decode HTML entities
        text = html_module.unescape(text)

        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:max_chars] if len(text) > 50 else None

    except Exception as e:
        log.debug(f"[web_search] Failed to fetch {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Company cache (backward-compatible upgrade)
# ---------------------------------------------------------------------------

def _load_company_cache() -> dict:
    """Load cached company research results.

    Handles both old format (plain strings) and new format (rich dicts).
    """
    if not COMPANY_CACHE_FILE.exists():
        return {}
    try:
        with open(COMPANY_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_company_cache(cache: dict):
    """Persist company research cache to disk."""
    COMPANY_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPANY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _get_cached_description(cache: dict, cache_key: str) -> str | None:
    """Get description from cache, handling both old and new format.

    Old format: {"company": "description string"}
    New format: {"company": {"description": "...", "website": "...", ...}}
    """
    if cache_key not in cache:
        return None
    entry = cache[cache_key]
    if isinstance(entry, str):
        return entry  # old format
    if isinstance(entry, dict):
        return entry.get("description", "")
    return None


# ---------------------------------------------------------------------------
# Website validation
# ---------------------------------------------------------------------------

def _validate_website_for_company(company_name: str, web_text: str) -> bool:
    """Check that the scraped website actually belongs to the target company.

    Strict matching: for short names (1-3 key words), ALL words must appear.
    This prevents e.g. 'KOWI Assurance AG' matching kowi.ch (fashion brand).
    """
    clean = strip_legal_suffixes(company_name)
    key_words = [w.lower() for w in clean.split() if len(w) > 2]

    if not key_words:
        return True  # Can't validate, allow it

    text_lower = web_text.lower()
    # Use word-boundary matching to prevent "SAP" matching "ASAP", etc.
    matches = sum(1 for w in key_words if re.search(rf'\b{re.escape(w)}\b', text_lower))

    # Short names (1-3 words): require ALL key words to match
    # Longer names (4+): require at least 75% to match
    if len(key_words) <= 3:
        return matches == len(key_words)
    return matches >= max(1, int(len(key_words) * 0.75))


# ---------------------------------------------------------------------------
# Helper: extract company domain from job URL
# ---------------------------------------------------------------------------

def _extract_company_domain_from_job_url(job_url: str) -> str | None:
    """If the job URL is on the company's own domain (not a job board), use it."""
    if not job_url:
        return None
    try:
        parsed = urlparse(job_url)
        domain = parsed.netloc.replace("www.", "").lower()
        if domain and not any(board in domain for board in JOB_BOARD_DOMAINS):
            return f"https://{parsed.netloc}"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Helper: extract company domain from email addresses in job description
# ---------------------------------------------------------------------------

# Generic email domains that don't reveal the company website
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "protonmail.com", "gmx.ch", "gmx.net", "bluewin.ch", "sunrise.ch",
    "hispeed.ch", "swissonline.ch", "aol.com", "mail.com", "zoho.com",
}


def _extract_domain_from_emails(description: str, company_name: str) -> str | None:
    """Extract the company website domain from email addresses in the job description.

    E.g. 'hr.ce@astara.com' → 'https://www.astara.com'
    E.g. 'sarah.camenisch@wwz.ch' → 'https://www.wwz.ch'

    Filters out generic email providers (gmail, hotmail, etc.).
    Validates that the domain relates to the company name.
    """
    if not description:
        return None

    # Find all email addresses in the description
    emails = re.findall(r'[\w.+-]+@([\w.-]+\.\w{2,})', description)
    if not emails:
        return None

    # Deduplicate and filter
    seen = set()
    candidates = []
    for domain in emails:
        domain_lower = domain.lower()
        if domain_lower in seen:
            continue
        seen.add(domain_lower)
        if domain_lower in GENERIC_EMAIL_DOMAINS:
            continue
        # Skip job board domains
        if any(board in domain_lower for board in JOB_BOARD_DOMAINS):
            continue
        candidates.append(domain_lower)

    if not candidates:
        return None

    # Strip legal suffixes from company name for matching
    clean_company = strip_legal_suffixes(company_name)
    company_words = [w.lower() for w in clean_company.split() if len(w) > 2]

    # Prefer domains where company name appears in the domain
    for domain in candidates:
        domain_base = domain.split('.')[0]  # e.g. "astara" from "astara.com"
        # Check if any company word matches the domain base
        if any(w in domain_base or domain_base in w for w in company_words):
            url = f"https://www.{domain}"
            log.info(f"  Company domain from email: {url}")
            return url

    # Don't fall back to unmatched domains — risk of using recruitment agency domain
    # instead of actual company website. Let downstream methods handle it.
    if candidates:
        log.info(f"  Email domains found ({candidates}) but none match company '{company_name}', skipping")
    return None


# ---------------------------------------------------------------------------
# Helper: extract 'About us' from job description
# ---------------------------------------------------------------------------

def _extract_about_from_description(description: str) -> str:
    """Extract 'About us' section or company intro from job description."""
    if not description or len(description) < 50:
        return ""

    about_headers = (
        r'(?:Über uns|Wer wir sind|Unser Unternehmen|Das Unternehmen|'
        r'About us|About the company|About|Who we are|'
        r'Chi siamo|L\'azienda)'
    )
    next_section = (
        r'(?:Aufgaben|Tasks|Anforderungen|Requirements|Ihre Aufgaben|Your Tasks|'
        r'Was wir bieten|What we offer|Dein Profil|Ihr Profil|Your Profile|'
        r'Deine Aufgaben|Ihre Vorteile|Benefits|Qualifikationen|Qualifications|'
        r'Wir bieten|Stellenbeschreibung|Das bringst du mit|Was du mitbringst|'
        r'Deine Verantwortung|Ihre Verantwortung|Das erwartet dich|Unser Angebot)'
    )
    pattern = rf'{about_headers}\s*[:\n]+(.*?)(?=\n\s*{next_section}|\Z)'
    match = re.search(pattern, description, re.IGNORECASE | re.DOTALL)
    if match:
        about = match.group(1).strip()
        if len(about) > 50:
            return about[:500]

    # Fallback: first 2 sentences often describe the company
    sentences = re.split(r'(?<=[.!?])\s+', description.strip()[:600])
    if len(sentences) >= 2 and len(sentences[0]) > 30:
        return ". ".join(sentences[:2]).strip()

    return ""


# ---------------------------------------------------------------------------
# URL guessing fallback (same as existing code in generate_cover_letter.py)
# ---------------------------------------------------------------------------

def _guess_company_urls(company: str) -> list[str]:
    """Generate likely website URLs for a Swiss company."""
    clean = strip_legal_suffixes(company)

    if not clean or len(clean) < 2:
        return []

    words = clean.split()
    candidates = []
    first = words[0].lower()
    full_hyphen = "-".join(w.lower() for w in words)
    full_concat = "".join(w.lower() for w in words)

    for name in [first, full_hyphen, full_concat]:
        name = (name.replace('ä', 'ae').replace('ö', 'oe')
                .replace('ü', 'ue').replace('ß', 'ss'))
        name = re.sub(r'[^a-z0-9-]', '', name)
        if name and name not in [c for c, _ in candidates]:
            candidates.append((name, None))

    urls = []
    seen = set()
    for name, _ in candidates:
        for tld in ['.ch', '.com', '.de']:
            url = f"https://www.{name}{tld}"
            if url not in seen:
                urls.append(url)
                seen.add(url)

    return urls[:9]


# ---------------------------------------------------------------------------
# LLM calling — delegates to shared llm_client
# ---------------------------------------------------------------------------

from execution.llm_client import call_llm as _call_llm_shared, get_openrouter_key, get_gemini_key
from execution.utils import strip_legal_suffixes, normalize_company_key


def _call_llm(prompt: str, temperature: float = 0.3) -> tuple[str, str]:
    """Call LLM for company research distillation. Returns (text, provider).

    Uses max_tokens=300 (lightweight, ~150-300 tokens for distillation).
    """
    openrouter_key = get_openrouter_key()
    gemini_key = get_gemini_key()
    return _call_llm_shared(openrouter_key, gemini_key, prompt, temperature=temperature, max_tokens=300)


# ---------------------------------------------------------------------------
# Main: research_company() — full pipeline
# ---------------------------------------------------------------------------

def research_company(
    company_name: str,
    location: str = "Switzerland",
    job_description: str = "",
    job_url: str = "",
    job_title: str = "",
    openrouter_key: str | None = None,
    gemini_key: str | None = None,
) -> dict:
    """Full company research pipeline. Returns:
    {
        'description': '2-3 sentence summary',
        'website': 'https://...' or None,
        'source': 'cache' | 'job_url' | 'email_domain' | 'google_search' | 'url_guess' | 'job_description',
        'confidence': 0.0-1.0
    }

    Search order:
    1. Cache (.tmp/company_cache.json)
    2. Job URL domain extraction (free, instant)
    3. Email domain extraction from description (free, reliable)
    4. URL guessing (free)
    5. Google Search with job context (1 SERP credit)
    6. Job description 'About us' extraction (free)
    7. LLM distillation of combined sources (~150 tokens)
    """
    cache = _load_company_cache()
    # Normalize cache key: strip legal suffixes so "Stadler AG" and "Stadler" share one entry
    cache_key = normalize_company_key(company_name)

    # 1. Check cache
    if cache_key in cache:
        entry = cache[cache_key]
        if isinstance(entry, dict):
            # Expire low-confidence entries older than 30 days (re-research may find them now)
            confidence = entry.get("confidence", 0)
            fetched_at = entry.get("fetched_at", "")
            if confidence < 0.5 and fetched_at:
                try:
                    age_days = (datetime.now() - datetime.strptime(fetched_at, "%Y-%m-%d")).days
                    if age_days > 30:
                        log.info(f"  Cache expired for '{company_name}' (confidence {confidence:.0%}, {age_days} days old) — re-researching")
                        del cache[cache_key]
                        _save_company_cache(cache)
                        # Fall through to re-research below
                    else:
                        desc = entry.get("description", "")
                        if desc:
                            log.info(f"  Company info (cached): {desc[:80]}...")
                        else:
                            log.info("  Company info (cached): none available")
                        return entry
                except (ValueError, TypeError):
                    pass  # Can't parse date, use cache as-is
            else:
                desc = entry.get("description", "")
                if desc:
                    log.info(f"  Company info (cached): {desc[:80]}...")
                else:
                    log.info("  Company info (cached): none available")
                return entry
        elif isinstance(entry, str):
            # Old format — still usable
            if entry:
                log.info(f"  Company info (cached, old format): {entry[:80]}...")
            return {
                "description": entry,
                "website": None,
                "source": "cache",
                "confidence": 0.6 if entry else 0.0,
            }

    # 2. Try job URL domain (free, most reliable)
    web_text = None
    website = None
    source = None
    company_domain = _extract_company_domain_from_job_url(job_url)
    if company_domain:
        web_text = fetch_page_text(company_domain)
        if web_text and _validate_website_for_company(company_name, web_text):
            website = company_domain
            source = "job_url"
            log.info(f"  Company website (from job URL): {company_domain}")
        else:
            if web_text:
                log.info(f"  Job URL domain {company_domain} doesn't match company '{company_name}', skipping")
            web_text = None

    # 3. Extract domain from email addresses in description (free, reliable)
    if not web_text and job_description:
        email_domain = _extract_domain_from_emails(job_description, company_name)
        if email_domain:
            fetched = fetch_page_text(email_domain)
            if fetched and _validate_website_for_company(company_name, fetched):
                web_text = fetched
                website = email_domain
                source = "email_domain"
                log.info(f"  Company website (from email in description): {email_domain}")
            elif fetched:
                log.info(f"  Email domain {email_domain} doesn't match '{company_name}', skipping")

    # 4. URL guessing (free — try before spending SERP credits)
    if not web_text:
        urls = _guess_company_urls(company_name)
        for url in urls:
            fetched = fetch_page_text(url)
            if fetched and _validate_website_for_company(company_name, fetched):
                web_text = fetched
                website = url
                source = "url_guess"
                log.info(f"  Company website (guessed URL): {url}")
                break
            elif fetched:
                log.info(f"  Website {url} doesn't match '{company_name}', skipping")

    # 5. Google Search with job context (1 SERP credit — only if free methods failed)
    if not web_text and _serpapi_keys:
        searched_url = search_company_website(company_name, location, job_title=job_title)
        if searched_url:
            fetched = fetch_page_text(searched_url)
            if fetched and _validate_website_for_company(company_name, fetched):
                web_text = fetched
                website = searched_url
                source = "google_search"
            elif fetched:
                log.info(f"  Google result {searched_url} doesn't match '{company_name}', skipping")

    # 6. Job description 'About us' extraction (free)
    about_text = _extract_about_from_description(job_description)

    # Build context for LLM distillation
    context_parts = []
    if about_text:
        context_parts.append(f"From job posting:\n{about_text}")
    if web_text:
        context_parts.append(f"From company website:\n{web_text}")

    if not context_parts:
        log.info(f"  No company info found for {company_name}")
        result = {
            "description": "",
            "website": None,
            "source": "none",
            "confidence": 0.0,
            "fetched_at": datetime.now().strftime("%Y-%m-%d"),
        }
        cache[cache_key] = result
        _save_company_cache(cache)
        return result

    # 6. LLM distillation (~150 tokens, cheap)
    context = "\n\n".join(context_parts)
    prompt = (
        f"Extract 2-3 key facts about {company_name} that a job applicant could reference "
        f"in a cover letter to show they researched the company.\n"
        f"Focus on: what the company does (products/services), their industry or market "
        f"position, and any notable values or achievements.\n"
        f"Return ONLY a 1-2 sentence summary (under 80 words). Example:\n"
        f"\"Graphax is Switzerland's leading independent provider of IT services and "
        f"digital printing solutions, serving businesses across the DACH region.\"\n\n"
        f"Company info:\n{context[:2000]}"
    )

    description = ""

    # Set confidence based on source quality (independent of description extraction)
    if source == "job_url":
        confidence = 0.95
    elif source == "email_domain":
        confidence = 0.85
    elif source == "google_search":
        confidence = 0.9
    elif source == "url_guess":
        confidence = 0.7
    elif source:
        confidence = 0.5  # job description only
    else:
        confidence = 0.3  # no source found

    try:
        result_text, provider = _call_llm(prompt, temperature=0.3)
        result_text = result_text.strip().strip('"').strip("'")
        if 20 < len(result_text) < 500:
            description = result_text
            log.info(f"  Company research ({provider}): {description[:100]}...")
    except Exception as e:
        log.warning(f"  Company research LLM failed: {e}")
        # Fallback: use raw about text
        description = about_text[:200] if about_text else ""
        if description:
            log.info(f"  Company info (description fallback): {description[:80]}...")

    if not source:
        source = "job_description" if about_text else "none"

    result = {
        "description": description,
        "website": website,
        "source": source,
        "confidence": confidence,
        "fetched_at": datetime.now().strftime("%Y-%m-%d"),
    }

    cache[cache_key] = result
    _save_company_cache(cache)
    return result


# ---------------------------------------------------------------------------
# Contact discovery — SerpAPI source for discover_contacts.py (Source 3)
# ---------------------------------------------------------------------------

def search_company_contacts(
    domain: str,
    company_name: str,
    max_serp_calls: int = 2,
) -> list[dict]:
    """Find HR / recruiter contacts for a company via Google search snippets.

    Strategy:
        1. `site:linkedin.com/in "{company_name}" ("Talent" OR "Recruiter" OR "HR")` —
           returns LinkedIn profiles whose headline mentions HR-adjacent roles.
        2. `"@{domain}" ("HR" OR "Recruiting" OR "Talent")` — returns directory pages,
           press releases, etc. that may carry an explicit recruiter email.

    Each query costs 1 SERP credit. Capped at `max_serp_calls` (default 2 = ~$0.005).

    Returns:
        List of contact dicts: `[{"name", "email", "title", "source_url", "source": "google"}, …]`.
        `email` is None for LinkedIn snippets (only name + title parseable).
        Empty list on any failure or no SerpAPI key.
    """
    domain_clean = (domain or "").strip().lower().lstrip("@").lstrip("/")
    if not domain_clean and not company_name:
        return []

    queries: list[str] = []
    if company_name:
        queries.append(
            f'site:linkedin.com/in "{company_name}" ("Talent" OR "Recruiter" OR "HR" OR "People")'
        )
    if domain_clean:
        queries.append(f'"@{domain_clean}" ("HR" OR "Recruiting" OR "Talent")')

    queries = queries[:max_serp_calls]
    if not queries:
        return []

    raw_hits: list[dict] = []
    for q in queries:
        results = search_google(q, num_results=3)
        for r in results:
            raw_hits.append({
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "link": r.get("link", ""),
                "query": q,
            })

    if not raw_hits:
        log.info(f"  search_company_contacts: no SERP hits for {company_name} / {domain_clean}")
        return []

    # LLM-parse the snippets in one batch — cheaper than per-result calls
    return _llm_parse_contact_serp_hits(raw_hits, company_name, domain_clean)


def _llm_parse_contact_serp_hits(
    hits: list[dict],
    company_name: str,
    domain: str,
) -> list[dict]:
    """Distill SERP snippets into structured contacts. Errors → []."""
    if not hits:
        return []

    # Build a compact prompt — title + snippet + link, indexed
    formatted = "\n\n".join(
        f"[{i}]\nTitle: {h['title']}\nSnippet: {h['snippet']}\nURL: {h['link']}"
        for i, h in enumerate(hits[:6])
    )

    prompt = (
        f"From these Google search results about people at {company_name} ({domain}), "
        "extract contact-relevant individuals (HR, recruiters, hiring managers, talent, "
        "people / culture roles). Return ONLY valid JSON:\n"
        '{"contacts": [{"name": "...", "email": "...", "title": "...", "source_index": 0}, ...]}\n\n'
        "Rules:\n"
        "- name: full name as it appears in the snippet, or null\n"
        f"- email: only if it explicitly contains '@{domain}' (skip otherwise — LinkedIn snippets "
        "  rarely include real emails). null if absent.\n"
        "- title: their role / job title, or null\n"
        "- source_index: which numbered result (0,1,2,...) supplied this person\n"
        "- Return at most 4 entries. Skip entries that look like job postings or company pages.\n"
        "- If nothing useful is found, return {\"contacts\": []}.\n\n"
        f"Search results:\n{formatted}"
    )

    try:
        from execution.llm_client import call_llm, parse_json_response
        response, provider = call_llm(
            os.getenv("OPEN_ROUTER_API_KEY"),
            os.getenv("GOOGLE_AI_STUDIO_API_KEY"),
            prompt,
            temperature=0.1,
            max_tokens=500,
            json_mode=True,
        )
        data = parse_json_response(response) or {}
        raw_contacts = data.get("contacts") or []
        out: list[dict] = []
        for entry in raw_contacts[:4]:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip() if isinstance(entry.get("name"), str) else None
            email = (entry.get("email") or "").strip() if isinstance(entry.get("email"), str) else None
            title = (entry.get("title") or "").strip() if isinstance(entry.get("title"), str) else None
            # Coerce sentinel strings to None
            for placeholder in ("null", "none", "n/a", ""):
                if name and name.lower() == placeholder:
                    name = None
                if email and email.lower() == placeholder:
                    email = None
                if title and title.lower() == placeholder:
                    title = None
            # Strip emails not on the right domain (LLM hallucination guard)
            if email and domain and f"@{domain}" not in email.lower():
                email = None
            if not (name or email):
                continue
            idx = entry.get("source_index")
            source_url = ""
            if isinstance(idx, int) and 0 <= idx < len(hits):
                source_url = hits[idx].get("link", "")
            out.append({
                "name": name,
                "email": email,
                "title": title,
                "source_url": source_url,
                "source": "google",
            })
        if out:
            log.info(f"  search_company_contacts: {len(out)} contact(s) via {provider}")
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  LLM parse of SERP contacts failed: {type(exc).__name__}: {exc}")
        return []
