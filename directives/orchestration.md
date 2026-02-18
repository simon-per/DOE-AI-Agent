# Directive: Pipeline Orchestration

## Goal
Run the 6-stage Swiss job application pipeline end-to-end. This is the master directive — read it first, then follow the stage-specific directives as needed.

## Pipeline Stages

```
Stage 1: Scrape     → raw_jobs.json
Stage 2: Evaluate   → scored_jobs.json
Stage 3: Sheet      → Google Sheet (deliverable)
Stage 4: Cover      → PDF + DOCX cover letters
Stage 5: CV         → PDF + DOCX tailored CVs
Stage 6: Follow-up  → Emails sent + sheet updated
```

## Quick Start (Full Run)

```bash
# Stage 1+2: Scrape and evaluate jobs
python execution/scrape_jobs.py

# Stage 2 (if running separately from scrape):
python execution/evaluate_jobs.py

# Stage 3: Write to Google Sheet
python execution/write_jobs_to_sheet.py

# Stage 4: Generate cover letters (score >= 6)
python execution/generate_cover_letter.py

# Stage 5: Generate CVs (score >= 6)
python execution/generate_cv.py

# Stage 6: Send follow-ups (after user marks jobs as "Applied" in sheet)
python execution/send_followups.py --send
```

## Stage Details

### Stage 1: Scrape Jobs
**Directive:** `directives/scrape_jobs.md`
**Script:** `execution/scrape_jobs.py`
**Input:** Search terms (hardcoded), SerpAPI keys
**Output:** `.tmp/raw_jobs.json`
**Budget:** ~51 SERP API calls per run (17 terms x 3 pages)

**Sources:** Indeed, LinkedIn, Glassdoor, Google Jobs (via python-jobspy + SerpAPI)
**Dedup:** URL normalization + fuzzy title/company matching across runs (`seen_jobs.json`)

**Common flags:**
- `--serp-pages 1` — minimal SERP budget (17 calls instead of 51)
- `--no-serp` — skip SerpAPI entirely, jobspy only
- `--hours-old 336` — 2 weeks instead of 30 days

### Stage 2: Evaluate Jobs
**Directive:** `directives/scrape_jobs.md` (evaluation section)
**Script:** `execution/evaluate_jobs.py`
**Input:** `.tmp/raw_jobs.json`
**Output:** `.tmp/scored_jobs.json`
**Budget:** ~1 LLM call per job (~200 tokens each)

**Scoring:** 1-10 scale based on skill match, language, degree requirements, location.
**Key thresholds:**
- Score 8-10: Strong match (green in sheet)
- Score 5-7: Moderate match (yellow)
- Score 1-4: Weak match (red) — skip for cover letters

### Stage 3: Write to Google Sheet
**Directive:** (none — straightforward)
**Script:** `execution/write_jobs_to_sheet.py`
**Input:** `.tmp/scored_jobs.json`
**Output:** Google Sheet "Swiss Job Search Pipeline"

**Columns:** 27 total — job info (11), reference data (3), application tracking (10), generation status (3).
**Dedup:** By URL (primary) or title+company (fallback). Append-only — never overwrites existing rows.

### Stage 4: Generate Cover Letters
**Directive:** `directives/generate_cover_letters.md`
**Script:** `execution/generate_cover_letter.py`
**Input:** `.tmp/scored_jobs.json`
**Output:** `.tmp/applications/{company}_{title}/Cover_Letter_*.pdf + .docx`
**Budget:** ~500 tokens per job (OpenRouter Qwen3 primary, Gemini Flash fallback)

**Quality gate:** Up to 3 LLM retries per job. Checks: hallucinations, word count (210-250), sentence burstiness, keyword coverage, company mention.
**Company research:** 7-step pipeline (cache → job URL → email → URL guess → Google → job desc → LLM). See `directives/web_search.md`.
**Sheet update:** Auto-sets `CL_Generated=Yes` and `CL_Quality_Score=X/5` in Google Sheet.

**Common flags:**
- `--limit 2` — test with 2 jobs
- `--min-score 8` — only top matches
- `--reset-checkpoint` — re-generate all

### Stage 5: Generate CVs
**Directive:** `directives/generate_cv.md`
**Script:** `execution/generate_cv.py`
**Input:** `.tmp/scored_jobs.json`
**Output:** `.tmp/applications/{company}_{title}/CV_*.pdf + .docx`
**Budget:** ~300 tokens per job (summary + skills + subtitle)

**Tailoring:** LLM generates summary/skills/subtitle. Experience bullets reordered by keyword relevance (deterministic, no LLM). Subtitle aligned with cover letter.
**Sheet update:** Auto-sets `CV_Generated=Yes` in Google Sheet.

**Common flags:** Same as Stage 4.

### Stage 6: Send Follow-Ups
**Directive:** `directives/send_followups.md`
**Script:** `execution/send_followups.py`
**Input:** Google Sheet (reads rows where Status=Applied, Date_Applied >= 3 days ago, Follow_Up_Sent=No)
**Output:** Gmail emails sent + sheet updated (Follow_Up_Sent=Yes, Follow_Up_Date)

**Prerequisites:** User must manually set Status="Applied", Date_Applied, and Contact_Email in the sheet.
**Common flags:**
- `--send` — actually send emails (without this, dry-run only)
- `--days 5` — wait 5 days instead of default 3

## Error Recovery

| Stage | Error | Recovery |
|-------|-------|----------|
| 1 | SERP API quota (429) | Wait 60s, retry. Use `--serp-pages 1` to reduce calls |
| 1 | Job-Room.ch SSL error | Skip (disabled). Other 5 sources still work |
| 2 | LLM rate limit | 5 retries with exponential backoff. Falls back Gemini → OpenRouter |
| 3 | Google Sheets quota (429) | Wait 60s, retry (3 attempts) |
| 4 | LLM returns hallucinations | Quality gate retries up to 3x with increasing temperature |
| 4 | Word count too short/long | Quality gate retries. Best attempt used as fallback |
| 4 | Company research fails | Falls back to job description extraction (confidence 0.5) |
| 5 | Playwright not installed | `python -m playwright install chromium` |
| 5 | JSON parse failure | Retries once, then uses safe defaults |
| 6 | Gmail auth missing | Run once without `--send` to trigger OAuth flow |

## Quality Thresholds

| Metric | Target | Hard Retry | Soft Warning |
|--------|--------|------------|--------------|
| Word count | 210-250 | < 190 or > 300 | < 200 or > 260 |
| Burstiness (StdDev) | > 8 | < 3 | < 5 |
| Keyword coverage | > 80% | — | < 50% |
| Hallucinations | 0 | Any found | — |
| Company mentioned | Yes | — | Not mentioned |

## File Organization

```
.tmp/
  raw_jobs.json          # Stage 1 output
  scored_jobs.json       # Stage 2 output
  company_cache.json     # Company research cache (persistent)
  seen_jobs.json         # Dedup across scrape runs (persistent)
  quality_report.json    # Stage 4 quality metrics
  cover_letter_checkpoint.json  # Stage 4 resume point
  cv_checkpoint.json     # Stage 5 resume point
  applications/          # Stage 4+5 output
    {company}_{title}/
      Cover_Letter_*.pdf
      Cover_Letter_*.docx
      CV_*.pdf
      CV_*.docx
```

## Related Directives

- `directives/scrape_jobs.md` — Stages 1-2 details, search terms, candidate profile, scoring
- `directives/generate_cover_letters.md` — Stage 4 details, prompt, quality checks
- `directives/generate_cv.md` — Stage 5 details, template, bullet reordering
- `directives/web_search.md` — Company research pipeline (used by Stage 4)
- `directives/send_followups.md` — Stage 6 details, email templates, Gmail setup
- `directives/setup_google_auth.md` — Google OAuth setup for Sheets + Gmail

## Learnings (auto-updated)
- Stages 4 and 5 auto-update the Google Sheet with generation status (CL_Generated, CL_Quality_Score, CV_Generated).
- Checkpoints allow resuming Stages 4 and 5 after interruption without re-generating already-processed jobs.
- Company research cache (.tmp/company_cache.json) persists across runs — most companies only need one SERP lookup ever.
- Quality gate prevents sending low-quality cover letters but uses the best available attempt as fallback (never blocks completely).
- **Recruitment agency detection** (Stages 4+5): When a company is a staffing agency (e.g., "impetus Personalberatung"), both cover letter and CV detect the actual employer (e.g., "WOLFFKRAN") from the job description. Both documents address the real company. Agency detection uses 3 patterns: bold markdown, "Für unseren Kunden", "Bei/At" prefix.
- **Title cleaning** (Stage 4): Raw job titles from scrapers may include URLs, percentages, and location suffixes. `_clean_title_for_subject()` strips these for the "Bewerbung als ..." subject line in PDFs/DOCXs.
- **Word count calibration:** Qwen3 undershoots by ~20 words in German. Prompt target is set higher (220-260) to achieve actual 200-250. Some descriptions with minimal context still produce short letters — this is an LLM limitation, not a bug.
- **Self-annealing verified:** 3 fixes (title cleaning, CV agency detection, confidence assignment) were implemented and re-tested. Run 3 results: 3/4 PASS, 1/4 WARNING (burstiness variance). WOLFFKRAN improved from WARNING 193w to PASS 202w. e-nov8 improved from WARNING 183w to PASS 202w.
