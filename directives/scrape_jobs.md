# Directive: Swiss Job Scraping Pipeline

## Goal
Scrape Swiss job listings matching the candidate profile, evaluate each for fit using Claude, and push scored results to a Google Sheet.

## Pipeline Overview

**Stage 1** → `execution/scrape_jobs.py` → `.tmp/raw_jobs.json`
**Stage 2** → `execution/evaluate_jobs.py` → `.tmp/scored_jobs.json`
**Stage 3** → `execution/write_jobs_to_sheet.py` → Google Sheet (deliverable, append-only)
**Stage 4** → `execution/generate_cover_letter.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 5** → `execution/generate_cv.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 6** → `execution/send_followups.py` → Emails sent + sheet updated

## Candidate Profile

- **Name:** Simon Oberpertinger Mair
- **Age:** 22 (soon 23)
- **Nationality:** Italian, relocating to Switzerland
- **Experience:** ~3 years as Revenue Operations Specialist (officially: Digital Sales Specialist) at Durst Group AG
- **Skills:** Microsoft Dynamics CRM (advanced), SAP MM/SD, Power BI, SQL, Excel, CPQ configuration, marketing automation
- **Certifications:** Google Data Analytics Professional Certificate, Microsoft Power BI Data Analyst Professional Certificate, SQL courses (Coursera)
- **Languages:** German (native), English (C1), Italian (medium)
- **Education:** Matura diploma (no bachelor/master)
- **Interests:** AI workflows (n8n, Claude Code), automation, data-driven decisions
- **Target roles:** Revenue Operations Specialist, Sales Operations Specialist, CRM Specialist, Business Analyst (CRM-adjacent)

## Search Configuration

### Search Terms
```
# Core CRM / Sales (English)
Revenue Operations Specialist
Sales Operations Specialist
CRM Specialist
Dynamics 365
Dynamics CRM
Sales Operations
Sales Analyst
Business Analyst CRM
CPQ Specialist
Marketing Automation Specialist

# German terms (Swiss market ~65% German-speaking)
CRM Berater
Verkaufsinnendienst
Digitalisierung Vertrieb

# Adjacent roles
Marketing Operations
Salesforce Administrator
HubSpot
Power BI Analyst
Revenue Operations
```

### Job Sources (priority order)
1. **python-jobspy** - Wraps Indeed, LinkedIn, Glassdoor, Google (free, no API key)
2. **SERP API** (Google Jobs) - Structured job data via Google Jobs index. 2 API keys × 250 free calls/month = 500 calls. Round-robin key rotation. Up to 3 pages/term (30 results). Smart pagination: stops early if all results on a page are already in `seen_jobs.json`.
3. **jobs4sales.ch** (jobchannel network, sales-specialized) - Next.js app; job listings served as JSON inside `__NEXT_DATA__` script tag. List URL: `https://jobs4sales.ch/en/jobs?q={term}&page={n}`. Detail URL: `https://jobs4sales.ch/en/job/{jobId}`. Up to 3 pages/term (30 results). `_niche_term_is_relevant()` filter skips terms outside the sales/CRM wheelhouse.
4. **ictjobs.ch** (jobchannel network, IT-specialized) - Server-rendered HTML cards in `<div class="offer publish">`. Search via `?fs={term}` on root URL (not `?q=`). Listing URLs carry `?fs=` suffix — stripped in parser for clean dedup. Same niche-term filter as jobs4sales.
5. **job-room.ch** (Swiss federal RAV/SECO board) - Direct API at `https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search` (POST). Unauthenticated; no Playwright needed. Body: `{"keywords":[term],"onlineSince":<days>,"workloadPercentageMin":10,"workloadPercentageMax":100,...}`. Response is a list with `jobAdvertisement.jobContent.{jobDescriptions[0].title|description, company.name, location, externalUrl}`. Many results are syndicated to other boards (jobs.ch etc.) but `externalUrl` points to the canonical employer/aggregator listing — high-quality direct postings, no lead-grab. Scraped sequentially across all SEARCH_TERMS in `main()` (not in the per-term thread pool). Skip with `--no-jobroom`.
6. **jobs.ch** (largest CH board) — Public search API at `https://www.jobs.ch/api/v1/public/search?query={term}&page={n}` (no auth). Returns JSON with `documents[]`. Per-doc: `title`, `company_name`, `place`, `preview`, `publication_date`, `initial_publication_date`, `age` (days), `_links.detail_de.href`, `employment_grades`. The `age` field is used to filter against `--hours-old` (rounded up to days); pagination stops as soon as a full page is over the cutoff (results sorted recency-first). Up to 5 pages/term. Skip with `--no-jobsch`.
7. **Public ATS boards** (Greenhouse, Lever) — curated CH employer list at `execution/data/ch_ats_employers.json`. Endpoints: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` and `https://api.lever.co/v0/postings/{slug}?mode=json`. Both unauthenticated, return JSON with dates and full descriptions. Only listings with a CH location pass the `_is_ch_location()` filter (canton/city tokens). **Verify slugs** before adding to the JSON: GET the endpoint and check for 200; many companies' careers-page text differs from their ATS slug. Skip with `--no-ats`.

### Freshness Window
- **Default:** 168h (7 days) — the recruiter shortlist for Swiss postings typically forms within ~7 days, so older listings have low conversion. Set via `DEFAULT_HOURS_OLD`.
- **Backfill:** `--deep` flag bumps to 720h (30 days). Use when seeding `seen_jobs.json` for the first time or recovering after a long pause.
- **Override:** `--hours-old N` sets an explicit value; if combined with `--deep`, the explicit value wins (and a warning is logged).
- All sources receive the same window: jobspy `hours_old`, SERP API `&date_posted=` mapping, jobroom `onlineSince` (rounded up to days), niche boards / ATS boards filter client-side.

### SERP API Budget
- 17 search terms × 3 pages = **51 calls per run** (max)
- Smart pagination reduces this on recurring runs (most results already seen)
- ~9 full runs/month with 500 budget (biweekly = ~102 calls/month, well within budget)
- CLI flags: `--serp-pages N` (default 3), `--no-serp` (skip entirely)

### Location
- Switzerland (all cantons)
- German-speaking cantons preferred

## Stage 1: Scraping

**Script:** `execution/scrape_jobs.py`
**Output:** `.tmp/raw_jobs.json`

### Normalized Job Schema
```json
{
  "title": "string",
  "company": "string",
  "location": "string",
  "url": "string",
  "description": "string (full text)",
  "source": "indeed|linkedin|glassdoor|google|serpapi/linkedin|serpapi/indeed|...",
  "date_posted": "ISO date or null",
  "salary": "string or null",
  "employment_type": "string or null"
}
```

### Deduplication
- **Within-run (2 layers):**
  1. Exact URL match (normalized: lowercase, strip trailing `/`)
  2. Fuzzy title+company match — strips gender markers `(m/f/d)`, percentage `80-100%`, legal suffixes (AG, GmbH, SA, Ltd...), parenthetical qualifiers `(Schweiz)` etc.
- **Cross-run:** `seen_jobs.json` stores **two hashes per job**:
  - `_job_hash` — normalized title + company (no URL, no location). Catches the same role surfaced on different aggregators.
  - `_job_content_hash` (`c:` prefix) — normalized title + company + location. Catches re-posts where the title was normalized inconsistently between runs but location matches.
  - On lookup, a job is "seen" if **either** hash matches an existing entry; both hashes are stored on write. Backward compat: legacy URL-inclusive entries are still honored on read (`_job_hash_legacy`).
  - Each entry now stores `{"first_seen": ts, "source": str}` so `execution/audit_aggregators.py` can compute per-source unique-hit ratios over a window. Pre-existing string-valued entries auto-migrate to `{"first_seen": <str>, "source": null}` on load.
- Always keeps the listing with the longest description

### Rate Limiting
- 2 second delay between requests to any single source
- Respect robots.txt where applicable

### Domain Blocklist
Some sources surface URLs that are useless downstream (paid aggregators, redirect-only domains). Those are dropped in `normalize_job()` before any description fetch / evaluation / sheet write happens, via the `BLOCKED_DOMAINS` frozenset in `execution/scrape_jobs.py`.

Matching: exact host **or** any subdomain — adding `"trabajo.org"` also blocks `ch.trabajo.org`, `www.trabajo.org`, etc.

Currently blocked:
- `jobleads.com` — paid aggregator, application requires payment
- `trabajo.org` — always 301-redirects to `jobleads.com`
- `bebee.com` — aggregator, hides company identity → CVs and cover letters can't be tailored, manual rewrite required per posting
- `experteer.com` — paid premium-subscription aggregator, listings effectively paywalled
- `talents.studysmarter.de` — expired listings / lead grab, not real openings
- `talent.com` — consistently redirects to jobleads.com (paid funnel), added 2026-04-30
- `cosmoquick.com` — user-excluded, added 2026-04-30

**Policy:** block only (a) paid aggregators and (b) aggregators that strip company identity. Free aggregators that preserve the company name (jooble.org, whatjobs.com, learn4good.com) are **intentionally kept** — they occasionally surface listings the primary sources miss.

**To extend:** append the bare domain (no scheme, no `www.`) to `BLOCKED_DOMAINS`. No other changes needed — the check runs for every scraped URL regardless of source (jobspy, SERP API, future scrapers).

### Aggregator Audit
`execution/audit_aggregators.py [--days 30]` reads `seen_jobs.json` and prints per-source job counts over the last N days. Use to decide whether a free aggregator (jooble.org, whatjobs.com, learn4good.com) is worth keeping — drop sources whose unique-hit share falls below ~5% of the total. Source data populates only after upgrading scrape runs (legacy entries from before the schema change show as "untagged").

## Stage 2: Evaluation

**Script:** `execution/evaluate_jobs.py`
**Input:** `.tmp/raw_jobs.json`
**Output:** `.tmp/scored_jobs.json`

### Pre-Filtering (Cost Optimization)
Before LLM evaluation, jobs are pre-filtered based on hard disqualifying criteria to reduce API costs:

**Auto-reject if:**
- Requires PhD/Doctorate degree (`phd`, `ph.d`, `doctorate`, `doktor`)
- Requires French language without German/English alternative
- Senior/Executive level (`senior manager`, `director`, `head of`, `vp`, `chief`, `executive`, `leiter`, `geschäftsführer`)
- Pure software engineering role (`software engineer`, `full stack`, `java developer`, `.net developer`, `devops engineer`, etc.)
- Internship/Praktikum (too junior for 3 years experience)
- No description available (can't evaluate, wastes LLM credits)
- Off-domain industries (title-only check, added with broader search terms):
  - Healthcare: `pflegefachperson`, `arzt`, `ärztin`, `therapeut`, `apotheker`, etc.
  - Manufacturing: `maschinenbauingenieur`, `konstrukteur`, `fertigungsleiter`, etc.
  - Legal/Finance: `rechtsanwalt`, `jurist`, `treuhänder`, `wirtschaftsprüfer`, etc.
  - Trades: `elektriker`, `schreiner`, `sanitärinstallateur`, etc.

**Impact:** Reduces LLM API calls by 20-40% on average. Rejected jobs are included in output with `score=0` and reason in `reasoning` field.

### Scoring Criteria (1-10)
- **Skills match** (CRM, SAP, BI tools, SQL, CPQ, marketing automation)
- **Experience level** (junior/mid ~3 years appropriate)
- **Language requirements** vs candidate languages
- **Degree requirements** — Bachelor ≈ 3 years experience (no penalty), only penalize for explicit Master/PhD requirement
- **Location accessibility**
- **Growth potential** (AI, automation, data-driven)

### Scoring Examples (anchoring)
- CRM Specialist / D365 Administrator, no degree required → **9**
- Business Analyst CRM, SQL + CRM experience, Bachelor's preferred → **8**
- Sales Operations Manager, 5+ years required → **5**
- SAP ABAP Developer, needs coding expertise → **3**
- Java Backend Engineer / Head of Marketing → **1**

### Model
- **Primary:** Qwen3 235B (`qwen/qwen3-235b-a22b-2507`) via OpenRouter (paid, cheapest, no rate limits with $10+ balance)
  - Env var: `OPEN_ROUTER_API_KEY`
  - Endpoint: `openrouter.ai/api/v1/chat/completions`
  - JSON mode enabled via `response_format: {type: "json_object"}`
  - X-Title header: `DOE AI Job Application Agent` (OpenRouter request prioritization)
  - Cost: $0.071/$0.10 per M tokens (input/output), no rate limits with $10+ balance
- **Fallback:** Gemini 3 Flash via Google AI Studio (free, rate-limited)
  - Env var: `GOOGLE_AI_STUDIO_API_KEY`
  - Endpoint: `generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent`
- If OpenRouter fails (API error, rate limit), automatically falls back to Gemini
- **2s delay** between API calls (prevents rate limits)
- **5 retries** on 429 with exponential backoff + jitter
- **Retry-After header** checked on 429 responses for optimal wait time
- Temperature: 0.1 (deterministic JSON output)
- **Rationale:** Qwen3 235B has zero rate limit errors with $10+ OpenRouter balance (dynamic RPS: $1 = 1 RPS). Previous model (Deepseek V3.2) was rate-limited even on paid accounts.

### Checkpointing
- **File:** `.tmp/evaluation_checkpoint.json` — tracks processed job IDs, allows resume on interruption
- `--reset-checkpoint` flag clears checkpoint and re-evaluates all jobs
- Jobs already in checkpoint are skipped on re-run (saves LLM credits)

### Glassdoor Exception
- Glassdoor jobs from python-jobspy often have empty descriptions (known limitation)
- These are **NOT auto-rejected** — they pass through to LLM evaluation based on title + company only
- Scores may be less reliable but prevents missing good matches

## Stage 3: Google Sheet (Append-Only)

**Script:** `execution/write_jobs_to_sheet.py`
**Input:** `.tmp/scored_jobs.json`
**Output:** Google Sheet (never overwrites existing rows)

### Columns
| Score | Title | Company | Location | Source | Key Matches | Key Gaps | Degree Required | Languages OK | Reasoning | Description | URL | Date Posted | Date Scraped | Status | Date_Applied | Application_Method | Contact_Person | Contact_Email | Follow_Up_Sent | Follow_Up_Date | Response_Date | Interview_Date | Notes |

### Application Tracking Columns (user-managed)
- **Status**: New → Applied → Follow-Up_Sent → Interviewing → Rejected/No_Response/Offer/Expired/Duplicate
- **Date_Applied**: User fills when applying (DD.MM.YYYY)
- **Application_Method**: Portal, email, recruiter, etc.
- **Contact_Person**: Auto-extracted from description (regex), user can override
- **Contact_Email**: User fills manually (used by Stage 6 follow-up system)
- **Follow_Up_Sent**: Auto-updated to "Yes" by Stage 6 when follow-up sent
- **Follow_Up_Date**: Auto-updated by Stage 6
- **Response_Date**: User fills when company responds
- **Interview_Date**: User fills when interview scheduled
- **Notes**: Free-form user notes

### Append-Only Behavior
- **Never clears existing rows** — preserves all user edits (Status, Notes, etc.)
- **Deduplication by URL** (column L): jobs already in sheet are skipped
- **Fallback dedup**: title+company match when URL is empty
- **Re-run safe**: "No new jobs to add" message if all jobs already in sheet
- New jobs are appended after the last existing row, sorted by score descending

### Formatting
- Sort by score descending (new jobs only)
- Conditional formatting: green (8-10), yellow (5-7), red (1-4)
- Bold header row with gray background
- Freeze header row

## Edge Cases & Learnings

- If jobs.ch API returns 403, skip it and rely on jobspy + Job-Room
- If python-jobspy fails on a specific board, log warning and continue with remaining sources
- If Claude returns malformed JSON, retry once, then skip job with score=0
- If Google Sheets API quota hit, wait 60s and retry

### Learnings (auto-updated)

- **Job-Room API** (`api.job-room.ch/jobAdvertisements/v1`) requires employer credentials -- not a public search API. The frontend endpoint (`ob.job-room.ch`) has SSL hostname mismatch (cert only valid for `www.job-room.ch`). The `www` endpoint returns empty 200s because it's an Angular SPA requiring browser-side CSRF tokens. Would need Playwright to scrape. Google Jobs already aggregates Job-Room listings via SerpAPI, so coverage gap is minimal.
- **jobs.ch** has no documented public API. Dropped from initial implementation; python-jobspy covers Indeed/LinkedIn/Glassdoor/Google which gives sufficient coverage.
- **Description truncation**: Claude evaluation truncates at 3000 chars. Logged when it happens.
- **Deduplication**: Fuzzy matching on normalized title+company (strips gender markers, legal suffixes, percentage ranges, parenthetical qualifiers). Keeps listing with longest description when duplicates found.
- **NaN handling**: jobspy returns pandas NaN. Use `pd.isna()` not string comparison.
- **Score range**: Failed evaluations return score=1 (not 0) to stay within 1-10.
- **JSON retry**: Evaluate script retries once on malformed JSON before defaulting.
- **Rate limit retry**: jobroom (3 retries, exponential backoff) and Claude API (3 retries on 429).
- **Sheets quota**: write script retries on 429 with 60s wait. Formatting failure is non-fatal.
- **gspread-formatting** required as additional dependency for conditional formatting.
- **Pre-filtering (cost optimization)**: Keyword-based filtering auto-rejects jobs requiring PhD, French language (without German/English), or senior/executive roles. Reduces API costs by 20-40%. Rejected jobs included in output with score=0.
- **SERP API integration**: Google Jobs endpoint (`engine=google_jobs`). Returns `company_name` (not `company`), `apply_options` array with direct/indirect links, `detected_extensions` for salary/schedule/posted_at. Round-robin key rotation is thread-safe. Smart pagination saves budget on recurring runs.
- **python-jobspy column rename**: v1.1+ renamed `company_name` → `company`. Always verify DataFrame column names after library updates.
- **Cross-run dedup**: `seen_jobs.json` tracks SHA-256 hashes of **normalized** title+company (URL excluded). Same job from Indeed vs LinkedIn produces same hash. `--reset-seen` clears history. Hash change from URL-based → normalized means first run after upgrade may re-evaluate some old listings (harmless).
- **Scoring calibration**: Degree bias fix — Bachelor ≈ 3 years experience (candidate has this). Only penalize for explicit Master/PhD. Concrete examples anchor the 1-10 scale to prevent score compression around 5-7.
- **SerpAPI Glassdoor source fix (2026-02-11):** Glassdoor exception checked `source == "glassdoor"` but SerpAPI returns `"serpapi/glassdoor"`. Changed to `"glassdoor" not in source` to match both.
- **SerpAPI relative date parsing:** `detected_extensions.posted_at` returns "2 days ago", "vor 1 Woche" etc. New `_parse_relative_date()` converts to ISO dates. Handles EN + DE relative strings.
- **URL param stripping for dedup:** Tracking params (utm_*, fbclid, gclid, mc_cid, mc_eid, ref, source) stripped before URL hashing. Same job from organic vs paid link now deduplicates correctly.
- **Normalization validation:** `normalize_job()` logs warnings for empty company/description fields so downstream stages know what to expect.
- **Checkpoint ID collision fix:** `_get_job_id()` in evaluate_jobs.py now includes URL SHA-256 hash: `{company}_{title}_{url_hash[:8]}`. Prevents collision when same company posts similar-titled jobs.
- **LLM response schema validation:** evaluate_jobs.py validates score (int, clamped 1-10), reasoning (str), key_matches (list), key_gaps (list) from LLM JSON. Catches malformed responses instead of silently producing incomplete evaluations.
- **jobs4sales.ch scraper (2026-04-22):** Next.js app — listings served as JSON in `<script id="__NEXT_DATA__">`. Navigate `props.initialState.public.jobs.jobs.jobs[]`. Fields: `jobId`, `title`, `companyName`, `location`, `datePosted` (ISO), `workload`. Pagination via `?page=N` (10/page). Stop when `page * pageSize >= resultCount`. Descriptions fetched in Phase 2 via detail URL `jobs4sales.ch/en/job/{jobId}`.
- **ictjobs.ch scraper (2026-04-22):** Server-rendered HTML. Search param is `fs=` (not `q=` — `?q=CRM` returns unfiltered homepage, `?fs=CRM` returns filtered results). Each listing wrapped in `<div class="offer publish">...</div><!--/.offer -->`. Title+URL in `<h2 itemprop="title"><a href="...">`, company in `<span class="author-text">`, location after `<span class="label-in">`, date in `<time datetime="YYYY-MM-DD">`. Strip `?fs=` query from detail URLs before normalize — otherwise the same job from different search terms won't dedupe.
- **Niche board term filter:** `_niche_term_is_relevant()` in scrape_jobs.py skips dev-only and off-profile terms before hitting jobchannel boards. Saves ~30% of HTTP requests on the ~28 configured search terms without losing signal.
- **Aggregator blocklist policy (2026-04-22):** block only (a) paid aggregators and (b) those that strip company identity. Free aggregators that preserve company name (talent.com, jooble.org, whatjobs.com, learn4good.com) are intentionally allowed — they occasionally surface unique listings. Added `experteer.com` (paid premium) and `talents.studysmarter.de` (expired/lead-grab) to BLOCKED_DOMAINS.
