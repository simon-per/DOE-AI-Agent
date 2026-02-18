# Directive: CV Generation

## Goal
Auto-generate tailored CVs (PDF + DOCX) for high-scoring jobs, with Swiss format (photo, personal details, work permit) and language matching the job posting. DOCX output allows manual editing before submission.

## Pipeline Position
**Stage 5** - runs after cover letter generation (Stage 4)

**Stage 1** → `execution/scrape_jobs.py` → `.tmp/raw_jobs.json`
**Stage 2** → `execution/evaluate_jobs.py` → `.tmp/scored_jobs.json`
**Stage 3** → `execution/write_jobs_to_sheet.py` → Google Sheet (append-only)
**Stage 4** → `execution/generate_cover_letter.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 5** → `execution/generate_cv.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 6** → `execution/send_followups.py` → Emails sent + sheet updated

## Configuration

- **Minimum score:** 6 (configurable via `--min-score`)
- **Language:** Auto-detected from job description (German/English/Italian)
- **Output format:** PDF (HTML/CSS → Playwright/Chromium) + DOCX (python-docx)
- **LLM:** OpenRouter (Qwen3 235B `qwen/qwen3-235b-a22b-2507`) primary, Gemini 3 Flash fallback
  - Qwen3: $0.071/$0.10 per M tokens, best German writing quality, no rate limits with $10+ balance
  - X-Title header: `DOE AI Job Application Agent` (OpenRouter request prioritization)
- **Temperature:** 0.3 (consistent, slightly tailored)

## Script: `execution/generate_cv.py`

**Input:** `.tmp/scored_jobs.json`
**Output:**
- `.tmp/applications/{company}_{title}/CV_Simon_Oberpertinger_Mair_{company}.pdf`
- `.tmp/applications/{company}_{title}/CV_Simon_Oberpertinger_Mair_{company}.docx`
**Template:** `execution/cv_template.html` (PDF only; DOCX uses python-docx directly)

### Usage
```bash
python execution/generate_cv.py                        # all jobs >= 6
python execution/generate_cv.py --limit 2              # test with 2
python execution/generate_cv.py --min-score 8          # only top matches
python execution/generate_cv.py --reset-checkpoint     # re-generate all
```

### Checkpointing
- **File:** `.tmp/cv_checkpoint.json` — tracks which jobs already have CVs
- Jobs in checkpoint are skipped on re-run (saves LLM credits + time)
- `--reset-checkpoint` clears checkpoint and re-generates all CVs
- Checkpoint saved after each successful generation (crash-safe)

### What Changes Per Job
**LLM-tailored** (3 things, keeps cost low):
1. **Professional summary** (3-4 sentences matching the job, with keyword mirroring — uses exact phrases from job posting where they match real skills)
2. **Skills order** (most relevant first based on key_matches)
3. **Subtitle** (professional title matching the role)

**Deterministically reordered** (no LLM cost):
4. **Experience bullets** — scored by keyword overlap with key_matches + description, most relevant bullets float to top
5. **Achievements** — same keyword-based reordering (CRM achievement first for CRM jobs, etc.)

Static: education, certs, languages, photo, personal info.

### Swiss CV Format (PDF)
- Profile photo (circular, in sidebar)
- Personal info: DOB, nationality (Italian/EU), work permit (EU/EFTA free movement)
- Contact: email, phone, location
- Sidebar: personal info, skills, languages
- Main: summary, experience, education, certifications
- All text localized to detected language (DE/EN/IT)

### DOCX Layout (Editable)
2-column table (no borders) mimicking the sidebar PDF layout:
- **Left column (5.5cm):** photo, personal info, skills (bullet list), languages, interests
- **Right column (12.5cm):** name (18pt), subtitle, summary, key achievements, experience (with bullets), education, certifications
- All content localized (DE/EN/IT) using same labels as PDF
- **Purpose:** User can edit text, reorder sections, or customize before submitting

### Output Structure (per job)
```
.tmp/applications/{company}_{title}/
├── Cover_Letter_Simon_Oberpertinger_Mair_{company}.pdf
├── Cover_Letter_Simon_Oberpertinger_Mair_{company}.docx
├── CV_Simon_Oberpertinger_Mair_{company}.pdf
└── CV_Simon_Oberpertinger_Mair_{company}.docx
```

### Privacy
- OpenRouter/Qwen3 receives anonymized profile (no name, email, phone, employer name)
- Google Gemini receives full profile
- Photo and PII are only in the PDF output, never sent to LLMs

## Dependencies
- `playwright>=1.40.0` - HTML to PDF via headless Chromium
- `jinja2>=3.1.0` - HTML template engine
- `python-docx>=1.1.0` - DOCX generation (editable Word documents)
- `langdetect>=1.0.9` - Language detection
- `requests` - LLM API calls
- `python-dotenv` - Environment variable loading

### Setup
After `pip install playwright`, run: `python -m playwright install chromium`

## Edge Cases & Learnings

### Quality Controls
1. **Anti-hallucination rules in CV_TAILOR_PROMPT:** Explicit allowed metrics whitelist, no invented numbers.
2. **Empty company skip:** Jobs with no company name skipped (can't produce professional tailored CV).
3. **Rate limit handling:** 5 retries with exponential backoff + jitter, Retry-After header, 2s delay between jobs.
4. **Skill reorder safety net:** After LLM returns skill order, deterministically re-sorts by key_matches relevance. Prevents odd LLM ordering where non-matching skills appear first.
5. **Subtitle consistency:** Reads cover letter checkpoint to use the same subtitle across both documents. Prevents "CRM Specialist" on cover letter vs "Digital Sales Manager" on CV.

### Learnings (auto-updated)
- weasyprint requires GTK/Pango system libraries on Windows — switched to Playwright which bundles Chromium
- Playwright needs `python -m playwright install chromium` after pip install
- Dynamic bullet/achievement reordering uses deterministic keyword scoring (no LLM cost). BULLET_KEYWORDS and ACHIEVEMENT_KEYWORDS map each item index to trigger keywords. Stable sort preserves original order for ties.
- Keyword mirroring in summary prompt: LLM echoes exact job posting terminology (e.g., "Kundenstammdatenpflege" instead of generic "CRM data management"). Helps ATS systems match CV to job.
- **DOB and education dates must match:** HTML template and DOCX code had different dates. These are now loaded from ABOUTME.md via profile_loader.py. Always verify both outputs match.
- **Hallucination in CVs too:** Summary/bullets can claim skills not in profile. Anti-hallucination rules added to CV_TAILOR_PROMPT with explicit allowed numbers.
- **Recruitment agency detection:** Imports `_detect_actual_employer()` from `generate_cover_letter.py` to ensure CV and cover letter address the same company. If company is "impetus Personalberatung" but the actual employer is "WOLFFKRAN", the CV header, filename, and folder all use "WOLFFKRAN". Gracefully falls back to raw company name if import fails.
- **Sheet auto-update:** After each CV generation, `update_job_columns()` sets `CV_Generated="Yes"` in the Google Sheet. Uses URL to find the correct row. Silently skips if sheet update fails (non-blocking).
- **Title cleaning not applied to folders:** Folder names use the raw `{company}_{title}` for uniqueness. Only the PDF/DOCX subject line is cleaned (done in Stage 4). Folder names with URLs/percentages look messy but are functionally harmless.
- **Line-height for German (2026-02-11):** Body `line-height: 1.45` was too tight for German text (longer words, more umlauts). Increased to `line-height: 1.55` in `cv_template.html`.
- **Photo path from env:** `CV_PHOTO_PATH` env var overrides the hardcoded photo path. Falls back to `.tmp/CV_picture 24.06.2025_sizeAdjusted.jpg`. Logs warning if photo not found.
