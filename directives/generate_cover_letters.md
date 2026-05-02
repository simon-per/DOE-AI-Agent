# Directive: Cover Letter Generation

## Goal
Auto-generate tailored cover letters (PDF + DOCX) for high-scoring jobs from the pipeline, with language matching the job posting. DOCX output allows manual editing before submission.

## Pipeline Position
**Stage 4** - runs after evaluation (Stage 2) produces `.tmp/scored_jobs.json`

**Stage 1** → `execution/scrape_jobs.py` → `.tmp/raw_jobs.json`
**Stage 2** → `execution/evaluate_jobs.py` → `.tmp/scored_jobs.json`
**Stage 3** → `execution/write_jobs_to_sheet.py` → Google Sheet (append-only)
**Stage 4** → `execution/generate_cover_letter.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 5** → `execution/generate_cv.py` → `.tmp/applications/{company}_{title}/*.pdf + *.docx`
**Stage 6** → `execution/send_followups.py` → Emails sent + sheet updated

## Configuration

- **Minimum score:** 6 (configurable via `--min-score`)
- **Language:** Auto-detected from job description (German/English/Italian)
- **Output format:** PDF + DOCX (both generated for every job)
- **LLM:** OpenRouter (Qwen3 235B `qwen/qwen3-235b-a22b-2507`) primary, Gemini 3 Flash fallback
  - Qwen3: $0.071/$0.10 per M tokens, best German writing quality, no rate limits with $10+ balance
  - Gemini 3 Flash: free tier fallback via Google AI Studio
  - X-Title header: `DOE AI Job Application Agent` (OpenRouter request prioritization)
- **Temperature:** 0.3 (low to reduce hallucinations while keeping natural phrasing)

## Script: `execution/generate_cover_letter.py`

**Input:** `.tmp/scored_jobs.json`
**Output:**
- `.tmp/applications/{company}_{title}/Cover_Letter_Simon_Oberpertinger_Mair_{company}.pdf`
- `.tmp/applications/{company}_{title}/Cover_Letter_Simon_Oberpertinger_Mair_{company}.docx`

### Usage
```bash
python execution/generate_cover_letter.py                        # all jobs >= 6
python execution/generate_cover_letter.py --limit 2              # test with 2
python execution/generate_cover_letter.py --min-score 8          # only top matches
python execution/generate_cover_letter.py --reset-checkpoint     # re-generate all
```

### Checkpointing
- **File:** `.tmp/cover_letter_checkpoint.json` — tracks which jobs already have cover letters
- Jobs in checkpoint are skipped on re-run (saves LLM credits + time)
- `--reset-checkpoint` clears checkpoint and re-generates all cover letters
- Checkpoint saved after each successful generation (crash-safe)

### Date Updates
Use `execution/update_cover_letter_dates.py` to re-render existing cover letter PDF + DOCX files with a new date without re-running the LLM.

```bash
python -m execution.update_cover_letter_dates --letter-date 2026-04-29 --min-score 6
python -m execution.update_cover_letter_dates --letter-date 2026-04-29 --min-score 6 --dry-run
python -m execution.update_cover_letter_dates --letter-date 2026-04-29 --min-score 6 --source local  # offline fallback
```

- **Default source:** Google Sheet `Swiss Job Search Pipeline`, not `.tmp/scored_jobs.json`.
- **Why:** The sheet is the application source of truth. Local scored JSON files are intermediate snapshots and can be stale or overwritten by later scrape/evaluation runs.
- **Selection:** Sheet rows with `Score >= --min-score` and an existing `.tmp/applications/{Job_ID}_*/Cover_Letter_Simon_Oberpertinger_Mair.docx`.
- **Safety:** The script renders PDF and DOCX to temporary files first, then replaces originals only after both render successfully. Any per-job error exits non-zero after logging.
- **Local mode:** `--source local` is only for offline/legacy use and computes missing `Job_ID` values from title/company/URL.

### Language Detection
Simple keyword-based detection:
- Counts German, English, Italian keywords in job description
- Returns highest-scoring language
- Defaults to German if tied

### What Changes Per Job
**LLM-tailored** (returns JSON with 2 fields):
1. **Cover letter text** (3-4 paragraphs, 200-250 words)
   - Company-personalized opening (uses company research)
   - Keyword mirroring from job description (e.g., "Kundenstammdatenpflege" not "CRM data management")
   - Sentence burstiness (varied length) to avoid AI detection
2. **Subtitle** (professional title matching the role, used in PDF header)

**Deterministic:**
3. **Company context** — hybrid research (cached):
   - Web scrape of company homepage (6 URL attempts: company.ch, company.com, etc.)
   - Extract "Über uns" section from job description
   - LLM distills 2-3 key facts (~150 tokens)
   - Cached in `.tmp/company_cache.json` (same company = 1 fetch)
4. **Contact person greeting** — regex extracts name from description (Swiss German patterns), LLM fallback when regex fails
5. **Language** — auto-detected from description (DE/EN/IT)

### PDF Layout
- Header: Candidate name, **tailored subtitle** (per job), contact info
- Date (right-aligned)
- Company name + location
- Subject line (e.g., "Bewerbung als CRM Manager")
- **Personalized greeting** (contact name if found, generic fallback otherwise)
- Body: 3-4 paragraphs generated by LLM
- Sign-off + signature

### DOCX Layout (Editable)
Same structure as PDF, generated with `python-docx`:
- Header: name (14pt bold), subtitle (9pt gray), contact info (9pt gray)
- Thin horizontal separator line
- Date (right-aligned, 10pt)
- Company name + location (10pt)
- Subject line (11pt bold)
- Greeting (10pt)
- Body paragraphs (10pt, justified)
- Sign-off + signature (bold)
- **Purpose:** User can edit text, fix LLM errors, or customize before submitting

### Cover Letter Strategy
- **200-250 words** (research: 250-word letters = 53% higher callback than 500+ words)
- **Company-personalized opening** (23-40% higher interview rate)
- **Keyword mirroring** from job description (+41% interview rate)
- **Sentence burstiness** to avoid AI detection (mix 5-8 word sentences with 20-30 word sentences)
- **Banned clichés:** "Mit grossem Interesse...", "Hiermit bewerbe ich mich...", "Auf der Suche nach..."
- **Swiss tells avoided:** No "ß" character (Swiss use "ss"), no starting 3+ sentences with "Ich"
- Highlights CRM, SAP, Power BI experience matching job requirements
- Uses key_matches from evaluation to focus on relevant skills
- Professional but personable tone
- Does NOT mention lack of university degree — focuses on value, not gaps

### Privacy
- OpenRouter/Qwen3 receives anonymized profile (no name, email, phone, employer name)
- Google Gemini receives full profile
- **`role_official` is CV-display-only and must never appear in LLM prompts.** Cover letters address the candidate's functional role, not the contract title. The contract title is intentionally stripped from both `profile_anonymous()` and `profile_full()`. If you add a new LLM-driven step, do NOT pass `role_official` to it.

## Dependencies
- `fpdf2>=2.7.0` - PDF generation with Unicode support
- `python-docx>=1.1.0` - DOCX generation (editable Word documents)
- `requests` - LLM API calls
- `python-dotenv` - Environment variable loading

## Edge Cases & Learnings

### Contact Person Extraction
- **Regex** (free, fast): 7 patterns covering German, Swiss German, English, Italian. Handles "Bei Fragen steht dir [Name]", "[Name], [Title], steht dir bei Fragen", "wenden Sie sich an [Name]" with optional words between.
- **LLM fallback** (cheap): When regex fails, sends last 800 chars to LLM asking for the contact name. ~100 tokens, validates result looks like a name (capitalized words, 2-3 words).
- Falls back to generic greeting ("Sehr geehrte Damen und Herren") only when both fail.

### Quality Gate System (Post-Generation)
The system retries LLM generation (up to 3 attempts, temperature +0.1 per retry) on **critical** quality failures. Soft issues are logged as warnings but don't block.

**Critical checks (trigger retry if failed):**
1. **Anti-hallucination validation:** Extracts all numbers from generated text, compares to whitelist of allowed metrics from candidate profile (230,000 / 500 / 20% / 100 / 200 / 30 / 45 / 3). Hallucinated numbers = instant retry.
2. **Hard word count bounds:** < 160 or > 300 words = retry (severely off). Soft warning at 180-280 range.
3. **Hard burstiness bound:** StdDev < 3 = retry (obviously AI). Soft warning at < 5.

**Soft checks (warnings only, no retry):**
4. **AI giveaway word filter:** Scans for banned AI-tell words (EN + DE lists) and auto-removes them.
5. **Swiss German enforcement:** Replaces all "ß" with "ss" (Swiss standard).
6. **ATS keyword coverage:** Checks that key_matches terms appear in generated text (fuzzy: any significant word from compound terms counts). Warns if < 50% coverage.
7. **Company mention check:** Verifies company name appears in the cover letter body. Warns if missing (generic letter).
8. **Empty company skip:** Jobs with no company name skipped entirely.

**Quality scoring:** Each attempt is scored 0-5 (hallucinations=3pts, word count=1pt, burstiness=1pt). Best attempt is used. Quality report written to `.tmp/quality_report.json` with per-job breakdown.

**Retry behavior:** Temperature increases 0.3 → 0.4 → 0.5 across attempts. Best-scoring attempt is selected even if all 3 fail quality gate.

### Company Research Validation
- **Job URL domain extraction:** If the job URL is on the company's own website (not a job board), uses that domain directly (most reliable).
- **Strict website validation:** For company names with 1-3 key words, ALL words must appear on the scraped website. Prevents false matches (e.g., "KOWI Assurance AG" matching kowi.ch fashion brand).
- **Known job board domains excluded:** indeed.com, linkedin.com, glassdoor.com, jobs.ch, jobup.ch, etc.

### Learnings (auto-updated)
- **Swiss German job postings** use varied contact patterns: "steht dir/Ihnen ... zur Verfügung", "wenden Sie sich an", "[Name], [Title], steht dir bei Fragen". Original 4 regex patterns missed all Swiss variants.
- **Cover letter prompt returning JSON** (text + subtitle) works reliably with Qwen3 235B. Fallback to plain text when JSON parsing fails.
- **Defensive degree mention** ("despite not having a university degree") draws attention to a gap unprompted — removed. Focus on value instead.
- **LLM contact extraction** can return non-name phrases ("hiring manager") — capitalization validation (each word starts with uppercase) prevents false positives.
- **Company personalization has massive ROI:** 23-40% higher interview rate (research-backed). Hybrid approach (web scrape + job description extraction + LLM distillation) works well.
- **Keyword mirroring = +41% interview rate** — echo exact job posting phrases (e.g., "Kundenstammdatenpflege" not "CRM data management") for ATS optimization.
- **Sentence burstiness prevents AI detection:** Swiss recruiters spot AI by uniform sentence length. Mix short (5-8 words) with long (20-30 words) sentences. StdDev of sentence lengths should be >8.
- **Qwen3 235B is the primary model:** Top-ranked on German proficiency benchmarks, no rate limits with $10+ OpenRouter balance. Gemini 3 Flash serves as free-tier fallback.
- **Swiss AI tell:** Using "ß" character (Eszett) immediately signals AI to Swiss recruiters — Switzerland uses "ss" instead.
- **Optimal length = 200-250 words:** Research shows 250-word letters have 53% higher callback rate than 500+ word letters. Swiss recruiters spend <30 seconds reading.
- **Hallucination is the #1 quality killer:** LLMs invent metrics (e.g., "reduced errors by 25%") that aren't in the candidate profile. Dual approach: prompt-level rules (explicit allowed numbers) + code-level validation (extract and compare).
- **Problem-Solution format** outperforms traditional intro-body-close for Swiss market. Open with company's challenge, immediately connect your experience as the solution.
- **KOWI-type false matches:** Company name "KOWI Assurance AG" matched kowi.ch (fashion brand) because "KOWI" appeared on page. Fix: require ALL key words for short company names (≤3 words).
- **Empty company = skip:** Jobs scraped without a company name produce unprofessional output (no address, no personalization). Better to skip than generate a bad letter.
- **Recruitment agency detection:** When the company field is a staffing agency (e.g., "impetus Personalberatung"), `_detect_actual_employer()` extracts the real employer from the description using 3 patterns: bold markdown (`**WOLFFKRAN**`), "Für unseren Kunden [COMPANY]", and "Bei/At [COMPANY]" in the first 300 chars. Section header false positives (e.g., `**Job Type:**`) are filtered via a 17-word blocklist. The cover letter then addresses the actual employer, not the agency.
- **Title cleaning for subject lines:** Raw job titles from scrapers can include URLs, percentages, and location suffixes (e.g., "Sales Specialist 80–100% | Meilen (ZH) | www.qsome.ch"). `_clean_title_for_subject()` strips these before using the title in "Bewerbung als ..." subject lines. Only applied to the PDF/DOCX subject line, not folder names.
- **Word count calibration for German:** Qwen3 consistently undershoots by ~20 words in German. Prompt target is set 10-20 words ABOVE actual desired range (prompt says 220-260, actual target is 200-250). English letters are closer to target. Some short job descriptions (minimal context) may still produce <200w despite retries — this is an LLM limitation.
- **Quality scoring partial credit:** Score distinguishes PASS (5/5) from WARNING (4-4.5/5). Word count 200-260 = 1pt, 190-300 = 0.5pt. Burstiness >=5 = 1pt, >=3 = 0.5pt. Sheet column shows score with word count (e.g., "4.5/5 (205w)") for quick review.
- **Small numbers (0, 1, 2) are whitelisted:** Too common to be hallucinations (e.g., "1-to-many engine", "B2B"). Added to ALLOWED_NUMBERS.
- **Company facts not flagged as hallucinations:** Hallucination checker now receives company research description as `extra_context`. Numbers from company descriptions (e.g., OTIS "2.5 million service units") are whitelisted per-job. Previously caused 3/3 retries to fail on OTIS.
- **Company mention check — TLD and word fallback:** Strips TLDs (.com, .ch, etc.) before matching (fixes "Huzzle.com" → "Huzzle"). Checks individual words for long names (fixes "Stadler Signalling Deutschland" matching on "Stadler"). Italian translations of English names still flagged correctly.
- **SimplyHired in filtered domains:** Added to JOB_BOARD_DOMAINS in web_search.py. Previously Google Search returned SimplyHired job listing URLs as company websites.
- **Confidence threshold (2026-02-11):** Company research data with confidence < 0.5 is now discarded before cover letter generation. Low-confidence entries (source: "job_description" at 0.3, "none" at 0.0) risk containing wrong-company info. The LLM writes without company context rather than wrong context.
- **Contact regex expanded:** Max name length 40→60 chars, max parts 3→4. Handles compound Swiss-German surnames (e.g., "Hans-Peter von Müller-Thurgau").
- **Word-boundary matching in URL validation:** `_validate_website_for_company()` uses `re.search(rf'\b{re.escape(w)}\b', text)` instead of `if w in text`. Prevents "SAP" matching "ASAP" etc.
- **URL guessing includes .de:** Added `.de` TLD for German/Swiss-German companies alongside `.ch` and `.com`.
- **Meta-commentary filter (2026-03-11):** LLMs (especially Qwen3) sometimes insert style meta-commentary to manipulate burstiness scores — e.g., "Short sentences work. Clarity matters." These are writing-advice artifacts, not cover letter content. `_filter_meta_commentary()` strips known patterns post-generation. The burstiness prompt was also softened to prevent the LLM from over-optimizing for sentence variation.
- **Empty parentheses in titles (2026-03-11):** `clean_job_title()` in `utils.py` now strips empty `()` left after percentage removal (e.g., "Senior Specialist (80-100%)" → "Senior Specialist ()" → "Senior Specialist"). Previously appeared in subject lines as "Application for ... ()".
- **Cover-letter date updates must be sheet-driven (2026-04-29):** `update_cover_letter_dates.py` previously read only `.tmp/scored_jobs.json`, which missed valid application folders when the JSON snapshot no longer contained older sheet rows (e.g., `J-8ce923`, `J-0fe4a7`). The script now defaults to the Google Sheet, matches folders by `Job_ID`, preserves existing DOCX subtitle/language/body, and keeps `--source local` only as an explicit offline fallback.
