# Directive: Follow-Up Email Automation (Multi-Touch)

## Goal
Send a sequence of three personalized follow-up emails per applied job, at 3 / 6 / 12
business days after `Date_Applied`. Multi-touch sequences typically double-to-triple
reply rates compared to a single follow-up — and Phase 2's Stage 4.5 contact discovery
means most rows actually have a real `Contact_Email` to reach.

## Pipeline Position
**Stage 6** — runs after user manually updates Google Sheet status to "Applied"

**Stage 1** → `execution/scrape_jobs.py` → `.tmp/raw_jobs.json`
**Stage 2** → `execution/evaluate_jobs.py` → `.tmp/scored_jobs.json`
**Stage 3** → `execution/write_jobs_to_sheet.py` → Google Sheet
**Stage 4** → `execution/generate_cover_letter.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 5** → `execution/generate_cv.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 6** → `execution/send_followups.py` → Emails sent + sheet updated

## Prerequisites

### One-time setup
1. **Enable Gmail API** in Google Cloud Console:
   - Go to https://console.cloud.google.com/apis/library/gmail.googleapis.com
   - Click "Enable"
2. The script uses a **separate token file** (`token_gmail.json`) from the sheet writer (`token.json`)
   - First run with `--send` will open browser for OAuth consent
   - Grant "Send email on your behalf" permission

### Per-job setup (user does manually in Google Sheet)
1. Change `Status` column to **"Applied"**
2. Fill in `Date_Applied` (format: DD.MM.YYYY)
3. Fill in `Contact_Email` (the email address to send follow-up to)
4. Optionally fill in `Contact_Person` (name for greeting personalization)

## Configuration

- **Cadence (fixed)**: 3 / 6 / 12 business days since `Date_Applied`. Each row may
  receive up to 3 touches total. After touch 3, no further follow-ups are sent —
  `prune_stale_jobs` eventually prunes old `Applied` rows after the separate
  `--applied-days` window (default 45 calendar days) when no response/interview
  marker exists.
- **Maximum wait**: 30 calendar days (configurable via `--max-days`). Sanity guard
  for very stale rows; covers touch 3 (12 bd ≈ 17 cal days) plus weekend slack.
- **DRY RUN by default**: must pass `--send` to actually send emails
- **Cloud trigger**: on-demand only via `modal run execution/modal_pipeline.py::pipeline_send_followups`
  after verifying contact recipients
- **Low-confidence skip gate** (`send_followups.py:552`): rows with
  `Contact_Confidence=low` are never auto-sent. This protects against Source 3
  `Constructed` emails (pattern-inferred guesses, never verified against the
  recipient inbox) reaching the wrong person. Reviewer must either replace the
  `Contact_Email` with a verified address (then clear the confidence cell, or set
  to `high`/`medium`) or trigger the send manually after sanity-checking.
- **LLM**: DeepSeek V4 Flash primary, Qwen3 235B fallback (all via OpenRouter — single billing surface)
- **Temperature**: 0.3 (slightly creative but consistent)
- **Rate limit**: 5s between email sends
- **Sender**: Configured via Gmail OAuth (the authenticated account)

## Three touch templates (selected by `touch_index` in the job dict)

- **Touch 1** — gentle ping (3 bd). Existing template, byte-identical to pre-Phase 2.
  `FOLLOWUP_PROMPT` in `send_followups.py`.
- **Touch 2** — value-add angle (6 bd). Less "checking in", more "here's something
  useful". Open with a concrete value-add (relevant insight from background, brief
  thought about a public company priority, or offer to share a case study link).
  Softer ask. `FOLLOWUP_PROMPT_TOUCH_2`.
- **Touch 3** — soft close (12 bd). Final touch. Polite, gracious, signals continued
  availability without pressure. `FOLLOWUP_PROMPT_TOUCH_3`.

All three share the same quality gate (15–200 word body, 5–120 char subject, JSON
parse + retry at temperature 0.5). The structural switch is purely the prompt;
Gmail send / sheet write logic is identical across touches.

## Script: `execution/send_followups.py`

**Input:** Google Sheet (reads rows with Status="Applied")
**Output:** Emails sent via Gmail API + sheet columns updated

### Usage
```bash
python execution/send_followups.py                    # DRY RUN (prints emails, doesn't send)
python execution/send_followups.py --send             # Actually send the touches due today
python execution/send_followups.py --max-days 21      # Skip rows older than 21 calendar days
python execution/send_followups.py --sheet-name "X"   # Custom sheet name
```

### Eligibility Criteria (per-row, per-run)
A row triggers a touch when ALL conditions are met:
1. `Status` = "Applied"
2. Has `Contact_Email` (valid format)
3. No `Response_Date` set
4. `Date_Applied` is parseable
5. Calendar age ≤ `--max-days` (default 30)
6. **Some touch is due today** — `_due_touch_index(date_applied, touch_count, today)`
   returns 1 / 2 / 3, not None. The function reads `Follow_Up_Count` (with backfill
   from `Follow_Up_Sent="Yes"` for legacy rows) and compares elapsed business days
   against the next threshold (3 / 6 / 12).
4. `Date_Applied` is at most `--max-days` days ago (default: 14)
5. `Response_Date` is empty (no response received yet)
6. `Contact_Email` is not empty

### What the LLM Generates
- **Subject line**: Brief, professional (in job's language)
- **Body**: 3-5 sentences, personalized:
  - References specific job title and company
  - Mentions application date
  - Expresses continued interest
  - Offers to provide additional information
  - Uses company context from cache (if available)
  - Matches job posting language (German/English)
  - Swiss style: no "ß", no generic clichés

### After Sending
The script updates the Google Sheet row:
- `Follow_Up_Sent` → "Yes"
- `Follow_Up_Date` → today's date (DD.MM.YYYY)

## Safety Features
- **DRY RUN default**: Must explicitly pass `--send` to send emails
- **Separate OAuth token**: `token_gmail.json` (doesn't affect sheet writer)
- **Email validation**: Skips rows without Contact_Email
- **Date validation**: Supports DD.MM.YYYY, YYYY-MM-DD, DD/MM/YYYY
- **Age limit**: Won't follow up on applications older than 14 days (configurable)
- **Rate limiting**: 5s between sends (Gmail daily limit: 500)
- **Error handling**: Failed sends logged, doesn't crash pipeline

## Dependencies
- `google-api-python-client` — Gmail API
- `google-auth-oauthlib` — OAuth 2.0
- `gspread` — Google Sheets API
- `requests` — LLM API calls
- `python-dotenv` — Environment variables

## Edge Cases & Learnings

### Date Parsing
- Swiss date format: DD.MM.YYYY (primary)
- Also supports YYYY-MM-DD and DD/MM/YYYY
- Invalid dates logged as warnings, row skipped

### Email Address
- User must manually enter `Contact_Email` in Google Sheet
- Script skips rows without email (logs info message)
- No auto-guessing of email addresses (too unreliable)

### Company Context
- Reuses `.tmp/company_cache.json` from cover letter generation
- If company not in cache, email is still generated (without specific company facts)

### Gmail API Quota
- Free tier: 500 emails/day
- Rate limited to 1 email per 5 seconds
- If quota exceeded, Gmail API returns 429 (script logs error and continues)

### Learnings (auto-updated)
- **Dynamic column indices (2026-02-11):** Hardcoded `COL_TITLE = 1`, `COL_STATUS = 14` etc. broke when columns were reordered. Now reads header row at runtime with `_build_column_map()` and `_col("Title")` accessor. Falls back to 0-based defaults if header lookup fails.
- **Language-aware contact fallback:** Generic "Hiring Manager" replaced with DE: "Personalverantwortliche/r", IT: "Responsabile delle risorse umane" when no contact name is available.
- **Idempotent email sending (2026-02-12):** Sheet updated BEFORE email is sent. If crash happens after sheet update but before send, rerun will skip (Follow_Up_Sent=Yes). Small risk of marking "sent" but email fails — acceptable vs duplicate emails.
- **Company cache is dict (2026-02-12):** Cache entries from web_search.py are `{"description": "...", "website": "...", "confidence": 0.9}`, not plain strings. `_get_company_context()` handles both formats (dict and legacy string).
- **Cache key normalization (2026-02-12):** Must strip AG/GmbH/SA/Ltd before cache lookup to match web_search.py keying. Uses `_normalize_cache_key()` with fallback to unnormalized key for backward compatibility.
- **Italian language detection (2026-02-12):** Three-way detection: German, English, Italian. Word-count based with configurable word lists.
- **Email quality gates (2026-02-12):** Body must be 15-200 words, subject 5-120 chars. Prevents sending empty or LLM-hallucinated mega-emails.
- **Checkpoint system (2026-02-12):** `.tmp/followup_checkpoint.json` tracks `(row_idx, contact_email)` tuples. Crash-safe, skips already-processed on rerun.
- **RFC email validation (2026-02-12):** Basic regex check before attempting to send. Prevents Gmail API errors on malformed addresses.
- **On-demand company research (2026-02-12):** When company not in cache, imports `research_company` from web_search.py to fetch context. Failures logged at WARNING level.
