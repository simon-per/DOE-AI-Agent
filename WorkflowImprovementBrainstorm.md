# Brainstorm: Pipeline Improvements

## 1. More Job Sources (biggest gap right now)

### SERP API (ready to go — keys already in `.env`)
- You have **2 keys × 250 free calls/month = 500 calls**
- SERP API’s **Google Jobs** engine returns structured job data:
  - title
  - company
  - description
  - link
  - date
- Coverage via Google’s index:
  - Google Jobs
  - Indeed
  - LinkedIn
  - Glassdoor
- Different angle than `python-jobspy` — catches listings jobspy misses
- **Budget**:
  - 9 search terms × 1 call each = **9 calls per run**
  - ~**55 runs/month**

### Niche Swiss Platforms (direct scraping or RSS)
- **jobs.ch**
  - Switzerland’s largest job board
  - Has RSS feeds per search
- **jobscout24.ch**
  - Popular in German-speaking Switzerland
- **ICTcareer.ch**
  - IT / digital roles specifically
- **arbeitgeber.ch**
  - Direct employer postings  
- These could be scraped via search pages or RSS where available

### Job-Room.ch Fix
- Currently disabled due to **SSL hostname mismatch**
- Worth investigating:
  - API endpoint change
  - New official URL
- Official Swiss government portal → **high-quality listings**

### Company Career Pages (targeted)
- For companies you’d love to work at, monitor careers pages directly  
- Examples:
  - Microsoft CH
  - Salesforce CH
  - SAP Switzerland
  - Swisscom
  - SBB
- Simple approach:
  - Fetch page
  - Diff against last fetch
  - Alert on new postings

---

## 2. Search Term Optimization

Current **9 terms are CRM-heavy**. Based on your profile (CRM + data analytics + automation), consider adding:

### Missing / High-Potential Terms
- **Marketing Operations** — CRM + automation overlap
- **Salesforce Administrator** — CRM-adjacent, huge market
- **HubSpot** — widely used in Swiss companies
- **Power BI Analyst** — you have the Microsoft cert
- **Data Analyst CRM** / **CRM Analytics**
- **Revenue Operations**
  - Previously noisy
  - Could work better with improved pre-filters
- **ERP Consultant**
  - Adjacent to SAP / D365 experience

### German-Language Terms (Switzerland ≈ 65% German-speaking)
- **CRM Berater** / **CRM Consultant**
- **Verkaufsinnendienst**
  - Inside sales, heavy CRM usage
- **Digitalisierung Vertrieb**
  - Sales digitization

---

## 3. Evaluation Improvements

### Smarter Scoring Prompt
- Current 1–10 scores cluster around **5–7**
- Add explicit anchoring:
  - **9–10** → perfect match, apply immediately
  - **3–4** → wrong field entirely
- Add weighted scoring:
  - Skills match: **40%**
  - Experience level fit: **25%**
  - Growth potential: **20%**
  - Location / language: **15%**

### Two-Pass Evaluation
- **Pass 1**: Quick screen
  - Cheap model
  - 1-sentence verdict
  - Score 1–10
- **Pass 2**: Deep evaluation
  - Only for scores ≥ 6
  - Better model
  - Detailed key matches & gaps
- Saves ~**50% LLM cost** on bad matches

### Negative Keyword Pre-Filter Expansion ✅
- **Implemented** (title-only checks to avoid false positives):
  - Healthcare: pflegefachperson, arzt, ärztin, therapeut, apotheker, etc.
  - Manufacturing: maschinenbauingenieur, konstrukteur, fertigungsleiter, etc.
  - Legal/Finance: rechtsanwalt, jurist, treuhänder, wirtschaftsprüfer, etc.
  - Trades: elektriker, schreiner, sanitärinstallateur, etc.

---

## 4. CV Improvements

Current CV: **single-page, single-role**

### Multi-Page Support (Senior Roles)
- Some Swiss employers expect **2 pages**
- Conditionally expand if enough content exists

### ATS Optimization (partially done)
- Many companies use:
  - Workday
  - SAP SuccessFactors
- Generate:
  - Plain-text or ATS-friendly CV
  - Designed PDF in parallel
- ✅ **Keyword mirroring**: LLM prompt now instructs to echo exact job posting phrases in summary (e.g., "Kundenstammdatenpflege" instead of generic "CRM data management")
- TODO: Generate plain-text / ATS-parseable CV variant in parallel with designed PDF

### Skills Visualization
- Replace plain bullet lists with:
  - Skill bars
  - Tag clouds
- Group by category:
  - CRM Tools
  - Analytics
  - Languages
  - Soft Skills

### Dynamic Achievements & Bullet Reordering ✅
- **Implemented**: Deterministic keyword-based reordering
  - Experience bullets scored by keyword overlap with `key_matches` + job description
  - Achievements reordered the same way (CRM achievement first for CRM jobs)
  - Stable sort preserves original order for ties, zero LLM cost

---

## 5. Cover Letter Improvements

### Contact Person Extraction ✅
- **Implemented**: Hybrid regex + LLM fallback
  - 7 regex patterns covering Swiss German, German, English, Italian
  - LLM fallback when regex fails (~100 tokens, very cheap)
  - Personalized greeting: "Guten Tag Marc Zeugin," instead of "Sehr geehrte Damen und Herren,"

### Tailored Header Subtitle ✅
- **Implemented**: LLM generates a job-specific subtitle alongside the cover letter text
  - Returns JSON: `{text, subtitle}`
  - Subtitle used in PDF header (e.g., "Dynamics CRM Entwickler & Datenanalyst" instead of generic "Digital Sales Specialist")

### Removed Defensive Degree Mention ✅
- **Implemented**: Prompt no longer instructs to address "lack of university degree"
  - Focuses on value (certifications, hands-on skills, continuous learning) instead of gaps

### Company Personalization ✅
- **Implemented**: Hybrid company research (web scrape + LLM extract + cache)
  - Fetches company website (6 URL attempts: .ch/.com variations)
  - Extracts "Über uns" section from job description
  - LLM distills 2-3 key facts (~150 tokens)
  - Cached in `.tmp/company_cache.json` (same company = 1 fetch)
  - Personalized opening references specific company details = 23-40% higher interview rate

### Keyword Mirroring in Cover Letter ✅
- **Implemented**: Added to cover letter prompt (already in CV)
  - Echoes exact job posting phrases (e.g., "Kundenstammdatenpflege" not "CRM data management")
  - +41% interview rate (research-backed)

### Anti-AI Detection (Burstiness) ✅
- **Implemented**: Sentence variation instruction in prompt
  - Mix short (5-8 words) with long (20-30 words) sentences
  - Natural human rhythm prevents AI detection
  - Swiss tell avoided: no "ß" character (use "ss")

### Model Upgrade: Qwen3 235B ✅
- **Implemented**: Switched from Deepseek V3.2 to Qwen3 235B A22B for cover letters
  - 72% cheaper ($0.078/M vs $0.28/M blended)
  - Top-ranked for German writing quality across multiple benchmarks
  - 3x faster (5-7 sec vs 15-20 sec per letter)
  - Better multilingual support (119 languages vs English-first Deepseek)

### Stronger Personalization
- Brief company research:
  - Company website
  - Wikipedia
- Reference:
  - Products
  - Values
  - Recent news
- Mention:
  - Why Switzerland
  - Why this specific city

### Format Variants
- Some companies prefer:
  - Short, email-style letters
- Others expect:
  - Traditional business letters
- Detect preferred tone from job posting

### A/B Testing
- Generate **2 versions per job**
- Different angles
- Track which style gets more responses over time

---

## 6. Pipeline Automation & Monitoring

### Scheduled Runs
- Currently manual
- Add weekly cron / Task Scheduler
- Example:
  - `scrape --hours-old 336` (2 weeks)
  - Biweekly schedule

### Application Tracking
- Track status after CV + cover letter generation:
  - Applied
  - Interview
  - Rejected
  - No Response
- Google Sheet:
  - New tab or column
- Dashboard with conversion rates

### Email Integration
- Auto-draft application emails
- Attach CV + cover letter
- Gmail API (Google OAuth already set up)
- **Human-in-the-loop**:
  - Draft only
  - You review and send

### Slack / Notification Alerts
- Example:
  - “3 new jobs scored 8+ found today”
- Already available:
  - Modal webhooks
  - Slack infrastructure

---

## 7. Quality of Life

### Web Dashboard
- Simple local UI to:
  - Browse results
  - Approve / reject
  - Trigger CV generation
- Tech options:
  - Streamlit
  - Flask

### Deduplication Improvements ✅
- ~~Current hash: title + company + URL~~
- **Implemented**: Fuzzy normalization on title + company
  - Strips gender markers (m/f/d), percentage ranges, legal suffixes (AG, GmbH...), parenthetical qualifiers
  - Cross-run hash uses normalized key (no URL) — same job from Indeed & LinkedIn now matches
  - Within-run dedup also uses fuzzy matching

### Cost Tracking
- Log API costs per run
  - OpenRouter returns usage in headers
- Monthly budget alerts