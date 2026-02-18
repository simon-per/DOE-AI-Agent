# Directive: Web Search Module

## Goal
Provide Google Search capabilities via SerpAPI for company research. Replaces fragile URL-guessing with intelligent search to find company websites for cover letter personalization.

## Module: `execution/web_search.py`

**Type:** Importable module (not a standalone pipeline script)
**Primary consumer:** `execution/generate_cover_letter.py` (Stage 4)

### Functions

```python
search_google(query, num_results=3)          # Raw Google Search (1 SERP credit)
search_company_website(company_name, location) # Find company website (1 SERP credit)
fetch_page_text(url, max_chars=5000)          # Fetch + extract clean text from URL
research_company(company_name, ...)           # Full pipeline with cache + search + LLM
```

### Usage
```python
from execution.web_search import research_company

result = research_company(
    company_name="KOWI Assurance AG",
    location="Switzerland",
    job_description="...",
    job_url="https://jobs.ch/...",
)
# result = {
#     "description": "KOWI Assurance AG is a Swiss insurance brokerage...",
#     "website": "https://www.kowi-assurance.ch",
#     "source": "google_search",
#     "confidence": 0.9,
#     "fetched_at": "2026-02-10"
# }
```

## Research Pipeline (search order)

1. **Cache** (free, instant) — `.tmp/company_cache.json`
2. **Job URL domain** (free) — extract domain from job posting URL, skip if job board
3. **Email domain extraction** (free) — extract company domain from email addresses in job description (e.g., `hr@astara.com` → `astara.com`)
4. **URL guessing** (free) — try `company.ch`, `company.com`, etc.
5. **Google Search with context** (1 SERP credit) — uses job title for industry context (e.g., "KOWI Assurance AG Sales Support Switzerland")
6. **Job description** (free) — extract "About us" section
7. **LLM distillation** (~150 tokens) — combine sources into 2-3 key facts

Free methods (steps 2-4) are tried before spending SERP credits (step 5).
Each step validates the website belongs to the target company (strict word matching).

## Budget

- **SerpAPI:** 500 calls/month (2 keys × 250), shared with job scraping
- **Job scraping:** ~51 calls/run (17 terms × 3 pages)
- **Company search:** ~1 call per unique company (cached after first lookup)
- **Typical run:** 20-50 unique companies = 20-50 SERP calls
- **Total per run:** ~70-100 calls → ~5 full runs/month
- **Cost:** Free tier (SerpAPI provides 100 free searches/month per key)

### Budget Guidelines
- Always check cache before searching
- Job URL domain extraction is free — prioritize it
- URL guessing is free — use as fallback before spending SERP credits
- Never search for the same company twice in one run (cache handles this)

## Cache Format

`.tmp/company_cache.json` — upgraded from plain strings to rich dicts:

```json
{
  "kowi assurance ag": {
    "description": "Swiss insurance brokerage...",
    "website": "https://www.kowi-assurance.ch",
    "source": "google_search",
    "confidence": 0.9,
    "fetched_at": "2026-02-10"
  }
}
```

**Backward-compatible:** Old format (plain string values) is auto-read. New entries use the rich format. Old entries are not migrated until next research for that company.

## Confidence Levels

| Source | Confidence | Why |
|--------|-----------|-----|
| Job URL domain | 0.95 | URL is from the job posting itself |
| Google Search | 0.90 | Google ranked it as the company site |
| Email domain | 0.85 | Domain from email in job posting (e.g., hr@company.com) |
| URL guessing | 0.70 | Might hit wrong domain |
| Job description only | 0.50 | No website verification |
| None found | 0.00 | No info available |

## Website Validation

Strict matching prevents false positives (e.g., "KOWI Assurance AG" matching kowi.ch fashion brand):
- For short company names (1-3 key words): **ALL** words must appear on page
- For longer names (4+ key words): **75%** of words must appear

## Filtered Domains

Job boards, social media, and directories are excluded from search results:
indeed, linkedin, glassdoor, jobs.ch, jobup.ch, google.com, wikipedia, facebook, twitter, youtube, kununu, moneyhouse, crunchbase, etc.

## Dependencies

- `requests` — HTTP calls (already installed)
- `python-dotenv` — Env var loading (already installed)
- SerpAPI keys: `SERPAPI_API_KEY`, `SERPAPI_API_KEY2` in `.env`

## Integration

In `generate_cover_letter.py`, `_research_company()` calls `web_search.research_company()` as the primary method. If the import fails or an error occurs, it falls back to the legacy URL-guessing approach.

## Learnings (auto-updated)
- SerpAPI `engine=google` for regular web search reuses the same API keys as `engine=google_jobs` for job scraping. No new API keys needed.
- Google Search with `gl=ch` (Switzerland) and `hl=de` (German) gives better results for Swiss companies.
- `nav`, `footer`, `header`, `aside` HTML tags should be stripped before text extraction — they contain navigation noise.
- **Pipeline order matters for budget:** URL guessing (free) should run before Google Search (1 SERP credit). Many Swiss companies have predictable domains (company.ch), so URL guessing succeeds ~60% of the time — saving SERP credits for harder cases.
- **HTTPError handling:** When catching `requests.exceptions.HTTPError`, use `e.response.status_code` instead of `resp.status_code` — the `resp` variable may not be bound if the error occurs early.
- **LLM response parsing:** Always wrap API response JSON traversal in try/except for `KeyError`/`IndexError`/`TypeError` — malformed responses from overloaded APIs happen occasionally.
- **Duplicate code is intentional:** `generate_cover_letter.py` keeps its own copy of URL guessing, validation, etc. as a legacy fallback. If `web_search.py` can't be imported, the cover letter pipeline still works.
- **Email domain extraction is highly reliable:** ~15% of Swiss job postings include contact emails (e.g., `hr.ce@astara.com`, `sarah.camenisch@wwz.ch`). The domain after @ is almost always the company's real website. Filter out generic providers (gmail, gmx, bluewin, etc.).
- **Google Search with job title context:** Adding industry keywords from the job title (e.g., "KOWI Assurance AG Sales Support Switzerland") helps Google find niche companies. Skip generic words like "specialist", "manager", "analyst".
- **KOWI-type companies may never be findable online:** Very niche local brokerages sometimes don't have a website. The pipeline correctly falls back to job description extraction — still generates usable company context for the cover letter.
- **Confidence assignment must be source-based, not description-dependent:** Bug found where confidence was assigned inside the `if description_extracted:` block. If a website blocked scraping (empty description), confidence stayed at 0.3 even for high-quality sources like email_domain (should be 0.85). Fix: confidence is now set based on source type independently of whether a description was successfully extracted.
- **SimplyHired added to JOB_BOARD_DOMAINS:** Google Search returned `simplyhired.ch/job/...` as a Stadler company website. Added simplyhired.com/ch/de to the blocklist. When adding new filtered domains, always add country variants (.com, .ch, .de).
- **Word-boundary matching (2026-02-11):** Substring matching (`if w in text`) causes false positives — "SAP" matches "ASAP", "Mercury" matches "emergency". Replaced with `re.search(rf'\b{re.escape(w)}\b', text_lower)` in `_validate_website_for_company()`. Applied in both web_search.py and generate_cover_letter.py.
- **Google Search fuzzy match tightened 50% → 75%:** For 4-word company names, 2/4 matching was too loose. Now requires 75% keyword match (`len(key_words) * 0.75`). For 2-word names, effectively requires both words.
- **Single-email-domain fallback removed:** Previously, if only one non-generic email domain was found but didn't match the company name, it was still returned. This caused recruitment agency emails to be treated as the company website. Now returns None if no name match.
- **Cache key strips legal suffixes:** AG, GmbH, SA, Sàrl, Ltd, Inc, SE, KG, OHG, Co, Gruppe, Group stripped before cache key generation. "Stadler AG" and "Stadler" now share one cache entry — saves SERP credits.
- **Cache expiration for low-confidence:** Entries with confidence < 0.5 are re-researched after 30 days. Prevents stale "not found" results from blocking better research.
- **URL guessing includes .de:** Added `.de` TLD for German/Swiss-German companies alongside `.ch` and `.com`. Max URLs increased from 6 to 9.
- **LLM distillation word limit:** Changed from conflicting "2-3 key facts" vs "under 50 words" to "1-2 sentence summary (under 80 words)".
