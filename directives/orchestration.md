# Directive: Pipeline Orchestration

## Goal
Run the 7-stage Swiss job application pipeline end-to-end. This is the master directive — read it first, then follow the stage-specific directives as needed.

## Pipeline Stages

```
Stage 1:   Scrape     → raw_jobs.json
Stage 2:   Evaluate   → scored_jobs.json
Stage 3:   Sheet      → Google Sheet (deliverable)
Stage 4:   Cover      → PDF + DOCX cover letters (gender-aware salutation)
Stage 4.5: Contacts   → Discover Contact_Email + Contact_Person from 5 free sources
Stage 5:   CV         → PDF + DOCX tailored CVs
Stage 6:   Follow-up  → 3-touch sequence (3 / 6 / 12 business days)
Stage 7:   Swarm      → Browser-based auto-apply (HITL)
+ Weekly digest       → Reply-rate metrics emailed every Sunday evening
```

## Execution Model: Cloud vs Local

The full preparation pipeline (Stages 1–6 + Stage 4.5 + weekly digest) runs
autonomously on Modal. Drive is the canonical fresh source for cover letters.
The laptop runs Stage 7 (apply) plus one transitional cron that re-stamps the
legacy local `.tmp/applications/` files for ad-hoc apply runs — both will
retire once Stage 7 fully reads from Drive.

| Where | Stages | Trigger |
|---|---|---|
| **Modal (cloud)** | **1–5 + 4.5 (full pipeline, scrape → CV)** | **Tue+Thu 18:00 CEST cron** — `pipeline_full`, `--min-score 6`, no manual gate. CLs stamped with that day's date during generation; uploaded to Drive automatically. |
| **Modal (cloud)** | 6 (send follow-ups, multi-touch) | Mon-Fri 08:00 CEST cron |
| **Modal (cloud)** | Maintenance: prune stale rows + archive Drive folders + reconcile orphans + hydrate Volume from Drive + re-stamp every score≥6 CL date → Drive | Daily 03:00 CEST / 02:00 CET — `pipeline_daily_maintenance`, `--min-score 6`, timeout 2h. Stages: `prune_stale_jobs --archive-drive --reconcile-orphans` (deletes pruned Sheet rows, moves their Drive folders to `DOE Applications/_Archive/` with an `archived_at` stamp, then reconciles: any **active** J-* folder whose `job_id` is no longer in the Sheet — manual deletes, legacy state — is also archived with `archive_reason: orphan`. Layered safeguards: empty-sheet guard, 10% threshold guard, 12h grace period for in-flight uploads, override via `--force-reconcile`. Permanently deletes folders that have been in `_Archive/` for 14+ days) → `hydrate_volume_from_drive --since-days 365` (downloads any active DOE Applications/J-* folder Drive has but the Volume is missing — best-effort, never blocks the next stage) → `update_cover_letter_dates` (mutates the right-aligned date paragraph in the DOCX in place; re-renders the PDF via fpdf2 from the body cached in `letter_meta.json` — no LLM, no template re-build). On every cron firing the OAuth health check (`_check_oauth_health`) runs first; failure emails `[DOE pipeline OAUTH FAIL]` and stops the run, day-5/6 age sends a `[OAUTH WARN]`. Coverage gap >5% triggers an alert email. Success summary email closes each run with hydrate / re-stamp / reconcile counts. Finishes before the 05:45 local apply window. |
| **Modal (cloud)** | Weekly digest | Sun 18:00 CEST cron |
| **Local (Windows Task Scheduler)** | Re-stamp legacy local `.tmp/applications/` CL dates (transitional) | Daily 05:45 local — `DOE_UpdateCoverLetterDates`, `--skip-drive --min-score 6`. Drive untouched (Modal owns it). Retire when Stage 7 pulls from Drive. |
| **Local (HITL)** | 7 (apply) | Run manually — Simon submits each application |

**Modal app:** `execution/modal_pipeline.py`
**Deploy:** `modal deploy execution/modal_pipeline.py`
**Manual trigger (cloud, full pipeline):** `modal run execution/modal_pipeline.py::pipeline_full`
**Connectivity check (cloud):** `modal run execution/modal_pipeline.py::preflight`
**Connectivity check (local):** `python execution/preflight.py`

**LinkedIn (jobspy):** disabled by default — set `DOE_ENABLE_LINKEDIN=1` to enable on local runs (LinkedIn 429-walls cloud egress, so it stays off on Modal).

**Search-term policy:** terms in `ABOUTME.md` are intentionally broad to surface the AI/automation framing across CRM, RevOps, Sales Ops, Marketing Ops, and BI. Quality is enforced at the **title-filter** layer in `scrape_jobs.py` (junior/clerical/operative + French/Italian) and the **scoring** layer (min 7), not by aggressive term pruning. Cut a term only when it returns verified-zero results over multiple runs.

**One-time Modal setup:**
1. `pip install modal && modal token new`
2. In Modal dashboard → Secrets → create `doe-google-oauth` with:
   - `TOKEN_JSON` = `base64 < token.json` (Sheets + drive.file scope, non-sensitive)
   - `CREDENTIALS_JSON` = `base64 < credentials.json`
3. Create `doe-api-keys` secret with `OPEN_ROUTER_API_KEY`, `SERPAPI_API_KEY`, `SERPAPI_API_KEY2`, plus mail vars:
   - `GMAIL_ADDRESS` = your Google account email
   - `GMAIL_APP_PASSWORD` = 16-char app password from <https://myaccount.google.com/apppasswords>
4. `modal deploy execution/modal_pipeline.py`

**Why no Gmail OAuth token?** Gmail send is via SMTP + app password (`execution/gmail_send.py`). App passwords don't expire, sidestepping Google's 7-day refresh-token rule for unverified sensitive scopes. `auth/drive.file` is similarly non-sensitive — same reason — so neither Drive nor Gmail trigger the recurring re-auth pain. Only the Sheets `auth/spreadsheets` scope still requires periodic re-auth until Google verifies the app.

**Re-auth procedure (when Sheets token expires).** The cloud `_check_oauth_health` function emails a `[DOE pipeline OAUTH WARN]` notice on day-5 / day-6 of token age and a `[DOE pipeline OAUTH FAIL] re-auth required` on day-7 hard expiry. To recover:
1. `python execution/write_jobs_to_sheet.py` — opens browser, refreshes `token.json`.
2. PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes('token.json'))` → copy output.
3. Modal dashboard → Secrets → `doe-google-oauth` → set `TOKEN_JSON` to the base64 value.
4. `modal deploy execution/modal_pipeline.py` — next cron firing picks up the new secret.

The on-Volume sentinel (`oauth_sentinel.json`) detects the rotation via SHA256 hash and resets the warning timer automatically.

## Reliability Hardening (Stages 4+5)

The cloud function `pipeline_full()` is the primary cloud entrypoint and invokes
the same Python scripts as local in the same order, so quality gates and
idempotency are identical by construction. On top of the local behavior, the cloud functions add:

**Preflight (`execution/preflight.py`)** — runs as the FIRST step of every
cloud invocation. 11 checks:
1. Profile parse (`ABOUTME.md` → `CandidateProfile`)
2. Sheets read (header columns present)
3. Sheets write (round-trip via dedicated `_preflight` tab)
4. Drive list (root `DOE Applications/` folder reachable)
5. Drive create+upload+delete (cycle through a temp `_preflight_<ts>` folder)
6. Gmail send (preflight ping email)
7. OpenRouter ping (`max_tokens=5`)
8. Gemini ping (`max_tokens=5`)
9. Volume sanity (`.tmp` write/read) — cloud only
10. Chromium launch — cloud only
11. Bundled assets present (ABOUTME, cv_photo, both CV templates) — cloud only

If any check fails, an alert email is sent to `simonobemair@gmail.com`, the
LLM stages do not run, and Modal marks the run as failed.

**Four dedup layers (in evaluation order):**
1. Checkpoint files (`.tmp/cover_letter_checkpoint.json`, `cv_checkpoint.json`) — primary
2. Sheet `--sheet-triggered` filter (`Status` column + `CV_Generated`) — script entry
3. **Folder-existence safety net** — globs `.tmp/applications/{job_id}_*/` for
   complete output sets (CL pdf+docx; CV pdf+docx + ATS pdf+docx). Catches the
   case where a checkpoint was lost but the artifact survived on the volume.
4. Drive name-dedup — `upload_application_folder()` skips files already in the
   target Drive folder

These run together. If layer 1 misses, layer 3 catches; if layer 3 misses,
layer 4 prevents Drive duplicates. The same `job_id` cannot produce the same
artifact twice across any combination of cron + manual runs.

**Failure visibility:** every `_run()` call in `modal_pipeline.py` is wrapped
so a non-zero subprocess exit triggers an email to `simonobemair@gmail.com`
with the failing stage label and the last 40 lines of output. The exception
is then re-raised so Modal also flags the run.

**Concurrency protection:** all scheduled functions use `max_containers=1`,
so a manual `modal run` cannot overlap a cron firing on the same function.

**Granular checkpointing:** `volume.commit()` is called after each stage in
multi-stage functions so partial progress (CL done, CV crashed) is not lost.

## Cloud Smoke Test (run before any change to Stages 4–5)

```bash
# 1. Local preflight — fastest first signal
python execution/preflight.py

# 2. Cloud preflight — confirms secrets + image + volume
modal run execution/modal_pipeline.py::preflight

# 3. End-to-end: full pipeline (scrape → score → CL → CV → contacts).
#    Generates everything for fresh jobs scoring >= 6 — no sheet gate needed.
modal run execution/modal_pipeline.py::pipeline_full

# 4. Idempotency check: trigger again immediately. Expect zero LLM calls,
#    zero new Drive uploads, no sheet diff. Folder dedup should fire.
modal run execution/modal_pipeline.py::pipeline_full
```

---

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

# Stage 7: Auto-apply via browser swarm (after marking jobs "Ready_to_Apply" in sheet)
python execution/run_swarm.py --limit 3
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

### Stage 4.5: Discover Contacts
**Script:** `execution/discover_contacts.py`
**Input:** Google Sheet (rows with `Status=APPLYING` and empty `Contact_Email`)
**Output:** Sheet columns `Contact_Person`, `Contact_Email`, `Contact_Source`, `Contact_Confidence`

**Five free sources, tried in order; first email wins:**
1. **Posting LLM extract** (`extract_contacts.py:extract_contacts_from_posting`) — strict JSON
   pull of name + email + title + honorific from the job description.
2. **Impressum / Kontakt scrape** (`contact_scraper.py:scrape_company_contacts`) — Playwright
   visits `/impressum`, `/kontakt`, `/team`, etc. (Swiss/DE/AT legal req → highest hit rate
   for SMEs). LLM second-pass for structured contacts.
3. **Google search via SerpAPI** (`web_search.py:search_company_contacts`) — `site:linkedin.com/in`
   queries scoped to recruiters / talent / HR; capped at 2 SERP credits per company.
4. **Email pattern + SMTP RCPT verify** (`email_verifier.py:verify_pattern`) — generates
   `firstname.lastname@`, `f.lastname@`, etc., MX-resolves the domain, probes via SMTP
   `RCPT TO`. Skips accept-all providers (Gmail/Outlook) → returns first pattern as low-conf
   guess instead of false-positive verify.
5. **NOT_FOUND** — sheet flagged so user can spot-check the worst 10% manually.

**Cache:** results stored in `.tmp/company_cache.json` per-company entry (30-day TTL).
**Failure mode:** every error path swallowed; the script always exits 0 so a slow
website / blocked port 25 / DNS hiccup cannot abort the parent pipeline. Worst case:
column reads `Contact_Source=NOT_FOUND` and the next 2-hour run retries the row.

**Cover letter integration:** when discovery returns an honorific (`Frau` / `Herr`), the
post-LLM `_greeting_for_contact()` in `generate_cover_letter.py` upgrades the salutation
to the formal `Sehr geehrte Frau {Lastname},` form. No LLM prompt change. Falls back
byte-for-byte to today's neutral salutation when no honorific is detected.

**Common flags:**
- `--sheet-triggered` — cloud default (filter to Status=APPLYING + empty Contact_Email)
- `--limit N` — process at most N rows
- `--dry-run` — log discoveries without writing to the sheet
- `--reset-cache` — ignore cached `contact_*` keys, rediscover

### Stage 6: Send Follow-Ups (multi-touch)
**Directive:** `directives/send_followups.md`
**Script:** `execution/send_followups.py`
**Input:** Google Sheet — rows where Status=Applied, has Contact_Email, no Response_Date
**Output:** Gmail emails sent + sheet updated (`Follow_Up_Sent=Yes`, `Follow_Up_Count`,
`Follow_Up_Last_Date`; `Follow_Up_Date` written on touch 1 only)

**3-touch cadence (business days since `Date_Applied`):**
- **Touch 1** — 3 bd: gentle ping (existing template, byte-identical)
- **Touch 2** — 6 bd: value-add angle (mention a relevant insight or offer to share more)
- **Touch 3** — 12 bd: soft close (final note, leaves the door open, signals availability)

**Cron:** Mon-Fri 08:00 CET. Each daily run picks up only the touches that came due that
day; running again the same day is a no-op (touch_count already incremented).

**Backwards-compat:** rows from before Phase 2 with empty `Follow_Up_Count` and
`Follow_Up_Sent=Yes` are interpreted as `touch_count=1` (touch 1 already done) → next
run sends touch 2.

**Prerequisites:** Status="Applied" + Date_Applied. Contact_Email is now usually filled
by Stage 4.5; user only needs to check the NOT_FOUND rows manually.

**Common flags:**
- `--send` — actually send emails (without this, dry-run only)
- `--max-days 30` — skip rows older than this many calendar days (default 30)
- `--reset-checkpoint` — clear local checkpoint and recheck all rows

### Weekly digest (no manual trigger)
**Script:** `execution/weekly_digest.py`
**Cron:** Sun 18:00 CET — read-only on the sheet, sends one plaintext email
**Output:** Reply rate by score band, by Contact_Source, by has-contact-name vs not;
follow-up touch distribution; top 5 quality scores this week; trend vs prior 4 weeks.
**Why it matters:** without outcome data, every "improvement" is a vibes call. After 2–3
weeks of digests, the data tells you which Phase 2 lever (contact discovery vs multi-touch
vs salutation) actually moved your reply rate. Phase 3 tuning is informed by this signal.

### Stage 7: Job Application Swarm (Multi-Agent)
**Directive:** `directives/apply_swarm.md`
**Script:** `execution/run_swarm.py`
**Input:** Google Sheet (reads rows where Status=Ready_to_Apply, newest batch first)
**Output:** Applications submitted + sheet updated (Status, Date_Applied, Application_Method)

**Architecture:** Multiple isolated Claude Code agents, each with its own Chrome browser via Chrome DevTools MCP. No separate API calls — Claude Code (subscription) is the brain.
**Prerequisites:** Stages 4+5 completed, user has set Status="Ready_to_Apply" in sheet, Chrome installed, `chrome-devtools-mcp` available via npx.
**Job selection:** Freshness > Rating — newest scrape batch first, then by score descending.
**Browser:** Headed Chrome with per-agent cloned profile (`.tmp/swarm/agent-N/profile/`) — selective cloning preserves logins.
**Rate limiting:** Platform-aware — LinkedIn/Workday: 1 agent, Indeed/Glassdoor: 2, others: 3.
**HITL:** Each agent is a live Claude Code session — asks human in terminal for unclear fields, always before Submit.

**Status flow:** `Ready_to_Apply` → `APPLYING` → `Applied` / `PAUSED` / `FAILED`

**Subcommands:**
- `setup --agents 3 --limit 9` — create agent workspaces, assign jobs
- `launch` — start Chrome instances on ports 9223+
- `status` — aggregate agent logs
- `kill` — terminate Chrome instances
- `cleanup` — update sheet from agent logs, remove workspace

**Common flags (setup):**
- `--agents 3` — number of agents (1-5, default: 3)
- `--limit 9` — max jobs total (default: 20, hard max: 20)
- `--all-batches` — process all batches, not just the newest

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
| 7 | Chrome not found | Install Chrome or update `CHROME_PATH` in `run_swarm.py` |
| 7 | CAPTCHA / login wall | Agent pauses (`[PAUSED]` in log) — resolve in Chrome window manually |
| 7 | CDP port unreachable | Run `kill` then `launch` again. Check firewall. |
| 7 | chrome-devtools-mcp missing | `npm install -g chrome-devtools-mcp` or ensure npx works |

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
  browser_profile/       # Stage 7 persistent Chrome profile (login once, reuse)
  swarm/                 # Stage 7 agent workspaces (setup creates, cleanup removes)
    tasks.json           # Read-only task assignments
    agent-N/             # Per-agent workspace (CLAUDE.md, .mcp.json, agent.log, profile/)
    screenshots/         # Before-submit + confirmation screenshots
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
- `directives/apply_swarm.md` — Stage 7 details, browser tools, HITL rules
- `directives/setup_google_auth.md` — Google OAuth setup for Sheets + Gmail

## Learnings (auto-updated)
- Stages 4+5 run on Modal as part of the unified `pipeline_full` (Tue/Thu 18:00 CEST) for any new job scoring >= 6. Four independent dedup layers (checkpoint, sheet filter, folder existence, Drive name-dedup) — the same `job_id` cannot produce duplicate artifacts even if checkpoints are wiped or two runs overlap. Date freshness on subsequent days is handled by `pipeline_daily_maintenance` (06:00 CEST) which re-stamps every score≥6 cover letter to today and pushes the refresh to Drive without re-running the LLM.
- A preflight check runs before any cloud LLM stage. Failures are emailed to `simonobemair@gmail.com` and the run aborts before burning tokens.
- `_write_oauth_files()` validates secret presence + base64 decode up front; missing/rotated secrets produce clear `Modal secret missing: <NAME>` errors instead of `binascii.Error`.
- `volume.commit()` runs after every stage so partial progress (e.g. CL done, CV crashes) survives a mid-run failure.
- Stages 4 and 5 auto-update the Google Sheet with generation status (CL_Generated, CL_Quality_Score, CV_Generated).
- Checkpoints allow resuming Stages 4 and 5 after interruption without re-generating already-processed jobs.
- Company research cache (.tmp/company_cache.json) persists across runs — most companies only need one SERP lookup ever.
- Quality gate prevents sending low-quality cover letters but uses the best available attempt as fallback (never blocks completely).
- **Recruitment agency detection** (Stages 4+5): When a company is a staffing agency (e.g., "impetus Personalberatung"), both cover letter and CV detect the actual employer (e.g., "WOLFFKRAN") from the job description. Both documents address the real company. Agency detection uses 3 patterns: bold markdown, "Für unseren Kunden", "Bei/At" prefix.
- **Title cleaning** (Stage 4): Raw job titles from scrapers may include URLs, percentages, and location suffixes. `_clean_title_for_subject()` strips these for the "Bewerbung als ..." subject line in PDFs/DOCXs.
- **Word count calibration:** Qwen3 undershoots by ~20 words in German. Prompt target is set higher (220-260) to achieve actual 200-250. Some descriptions with minimal context still produce short letters — this is an LLM limitation, not a bug.
- **Self-annealing verified:** 3 fixes (title cleaning, CV agency detection, confidence assignment) were implemented and re-tested. Run 3 results: 3/4 PASS, 1/4 WARNING (burstiness variance). WOLFFKRAN improved from WARNING 193w to PASS 202w. e-nov8 improved from WARNING 183w to PASS 202w.
- **Silent-success in `pipeline_daily_maintenance` (fixed 2026-05-07):** the Modal `doe-tmp` Volume only contains folders Modal generated — legacy local-generated `J-*` folders lived on Drive but not on the Volume. `update_cover_letter_dates.py` walked the Volume, silently skipped the missing folders as `missing_folder`, and exited 0 because the 5%-error-rate gate only counts true render errors. Fix: a new `hydrate_volume_from_drive.py` stage runs before the re-stamp and incrementally pulls Drive→Volume (idempotent, size-match skip). The script's main loop also now logs a coverage % and emails an alert when `(missing_folder + missing_docx) / eligible > 5%`. Sheet-load filter requires `CL_Generated=Yes` so freshly-scraped score≥6 rows don't trigger false alerts.
- **In-place DOCX date edit + JSON-backed PDF render (2026-05-07):** the daily re-stamp no longer re-renders the DOCX from scratch. `generate_cover_letter.py` now writes a `letter_meta.json` sidecar (body, contact_name, subtitle, language, first_letter_date) into each `.tmp/applications/{folder}/` at generation time. `update_cover_letter_dates.py:_restamp_atomically` reads that JSON, mutates the right-aligned `DD.MM.JJJJ` paragraph in the DOCX in place (no body extraction, no template re-build), and re-renders the PDF via fpdf2. Legacy folders without the sidecar fall back to the regex body extraction once and write the sidecar so the second daily run is on the fast path. Removes the regex fragility that could cause empty-body errors and saves the `generate_docx` template re-build on every folder.
- **Round 2 reliability hardening (2026-05-07):** Drive API + Sheets API calls now retry on `{429, 500, 502, 503}` with exponential backoff (1/2/4/8 s, 5 tries). Hydrate runs 8 parallel downloads (`ThreadPoolExecutor`) and is now best-effort — its failure is decoupled from the re-stamp via `_run(..., check=False, timeout=2400)`. Subprocess hangs are killed by a `threading.Timer` watchdog on every cloud cron stage (timeouts 270 s–3600 s per stage). A daily `[DOE pipeline OK] daily maintenance` email confirms the cron actually ran — silence is now the failure signal. Stale `.part` files older than 24 h are swept on every hydrate startup. `_sheets_api_call` is now used uniformly across `send_followups.py` and `update_cover_letter_dates.py` — no more raw gspread calls bypassing retry.
- **Round 3 lifecycle + OAuth (2026-05-07):** every cloud cron now runs `_check_oauth_health` at startup — actively reads the Sheet header, hashes `token.json` to detect rotation via `oauth_sentinel.json` on Volume, sends `[OAUTH WARN]` once on day-5 and once on day-6, hard-fails with `[OAUTH FAIL]` from day-7. Drive folders for pruned rows now move to `DOE Applications/_Archive/` (stamped with `appProperties.archived_at`); folders older than 14 days are permanently deleted on the next prune run. `prune_stale_jobs.py --archive-drive` is the cloud-only opt-in (local runs continue to use `--skip-drive` parity). Schema-drift errors in `update_cover_letter_dates.py` now print all available headers + a 30-second remediation hint instead of a one-line opaque RuntimeError.
- **Round 4 orphan reconcile (2026-05-07):** `prune_stale_jobs.py --reconcile-orphans` adds a Drive→Sheet sweep that archives any **active** `J-*` folder whose `job_id` is no longer present in the Sheet (covers manual row deletions and pre-`--archive-drive` legacy state). Layered safeguards prevent accidental mass-archives: (1) abort with `aborted=empty_sheet` if the Sheet read returns zero job_ids — assumes a Sheet API failure; (2) abort with `aborted=threshold` if more than `--orphan-max-pct` (default 10%) of active folders would be archived — assumes a Sheet read corruption; (3) `--orphan-grace-hours` (default 12) skips folders modified recently to avoid clobbering in-flight CL uploads from `pipeline_full`. Override (1)+(2) via `--force-reconcile` when a large legitimate cleanup is expected. Orphan archives use `appProperties.archive_reason="orphan"` (vs default `"pruned"` for explicit deletions) and follow the same 14-day permanent-delete window. The daily success summary email now includes `Reconcile: active=N orphans=N archived=N aborted=...` so safeguard trips are immediately visible.
