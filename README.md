# DOE AI Agent — Automated Job Application Pipeline

> An AI-powered system that scrapes, evaluates, and applies to jobs autonomously — built to demonstrate how Sales Operations workflows can be automated end-to-end.

**[Deutsch weiter unten](#deutsch)**

---

## The Problem

Searching for a job in Switzerland means spending hours every day on repetitive tasks: scanning job boards, reading descriptions, deciding which roles fit, writing tailored cover letters in the right language, updating your CV for each application, and following up weeks later. Most of this is manual, slow, and error-prone.

## The Solution

This project automates the entire job application workflow — from scraping 29 search terms across multiple job boards to generating personalized, language-matched cover letters and CVs, tracking everything in a Google Sheet, and sending timed follow-up emails.

The system processes **1,000+ job listings per run**, evaluates each one against a personal profile, and generates application documents for the best matches — all without manual intervention.

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    6-STAGE PIPELINE                          │
│                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────┐    │
│  │ 1. SCRAPE│──▶│2. EVALUATE│──▶│ 3. WRITE TO SHEET   │    │
│  │ Job Boards│  │ with LLM  │   │ (Google Sheets)      │    │
│  └──────────┘   └──────────┘   └──────────────────────┘    │
│       │                                    │                │
│       │         ┌──────────┐   ┌──────────────────────┐    │
│       │         │5. GEN CV │◀──│ 4. GEN COVER LETTER  │    │
│       │         │ (PDF+DOCX)│  │ (PDF+DOCX)           │    │
│       │         └──────────┘   └──────────────────────┘    │
│       │                │                                    │
│       │         ┌──────────────────────┐                   │
│       │         │ 6. FOLLOW-UP EMAILS  │                   │
│       │         │ (after N business days)│                  │
│       │         └──────────────────────┘                   │
│       │                                                     │
│  ┌────▼────────────────────────────────────────────────┐   │
│  │              SHARED INFRASTRUCTURE                   │   │
│  │  LLM Client · Language Detection · Profile Loader   │   │
│  │  Contact Extraction · Company Research · Utils       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Stage 1 — Scrape
Searches multiple job boards (Indeed, Glassdoor, LinkedIn, etc.) using 29 targeted search terms. Deduplicates results, normalizes data, and fetches missing job descriptions directly from employer websites.

### Stage 2 — Evaluate
An LLM scores each job (0–100) based on fit with the candidate's profile — considering skills, experience, location, and language. Jobs below the threshold are filtered out. Checkpointed so it can resume after interruptions.

### Stage 3 — Write to Google Sheet
Ranked jobs are written to a Google Sheet with full tracking columns: Score, Job ID, Title, Company, Location, Status, Contact info, Follow-up dates. The sheet is append-only with URL-based deduplication.

### Stage 4 — Generate Cover Letters
For each top-scoring job, the system generates a tailored cover letter in the detected language (German, English, or Italian). Output: PDF + DOCX per application. Quality gates ensure word count, tone, and formatting meet Swiss market standards.

### Stage 5 — Generate CVs
A matching CV is generated for each application, with skills reordered by relevance to the specific job. The language is kept consistent with the cover letter. Output: PDF + DOCX.

### Stage 6 — Follow-Up Emails
After a configurable number of business days, the system drafts and sends personalized follow-up emails. Dry-run by default — nothing is sent without explicit confirmation.

## Key Design Decisions

| Decision | Why |
|----------|-----|
| **Deterministic scripts, not AI for everything** | LLMs are probabilistic. Business logic (dedup, formatting, sheet writes) must be reliable. AI is only used where judgment is needed (evaluation, writing). |
| **Tri-lingual support (DE/EN/IT)** | Swiss job market requires German, English, and Italian. Language is auto-detected per job posting. |
| **Checkpoint everything** | Every stage saves progress. A crash at job #150 doesn't re-process the first 149. |
| **Primary key system (Job ID)** | Deterministic `J-XXXXXX` hash links sheet rows, application folders, and checkpoints — no manual matching needed. |
| **Privacy-aware LLM routing** | Personal data only goes to trusted providers (Google Gemini). External providers receive an anonymized profile. |
| **Dry-run by default** | Follow-up emails, sheet clears, and migrations all default to dry-run. Destructive actions require explicit flags. |

## Tech Stack

- **Python 3.11+** — All pipeline scripts
- **LLMs** — Qwen3-235B (via OpenRouter) + Gemini Flash (Google AI Studio) as fallback
- **Google Sheets API** — Application tracking and status management
- **Gmail API** — Follow-up email delivery
- **python-jobspy** — Multi-board job scraping
- **ReportLab** — PDF generation for CVs and cover letters
- **python-docx** — DOCX generation

## Project Structure

```
├── execution/              # Deterministic Python scripts (the engine)
│   ├── scrape_jobs.py      # Stage 1: Multi-board job scraping
│   ├── evaluate_jobs.py    # Stage 2: LLM-based job scoring
│   ├── write_jobs_to_sheet.py  # Stage 3: Google Sheets integration
│   ├── generate_cover_letter.py # Stage 4: Tailored cover letters
│   ├── generate_cv.py      # Stage 5: Job-specific CVs
│   ├── send_followups.py   # Stage 6: Timed follow-up emails
│   ├── llm_client.py       # Shared: LLM call + retry logic
│   ├── language_detect.py  # Shared: DE/EN/IT detection
│   ├── profile_loader.py   # Shared: ABOUTME.md parser
│   ├── extract_contacts.py # Shared: Contact info extraction
│   ├── web_search.py       # Shared: Company research
│   └── utils.py            # Shared: Filename sanitization, Job ID generation
├── directives/             # SOPs in Markdown (what each stage should do)
├── ABOUTME.example.md      # Template for personal profile (real data gitignored)
├── CLAUDE.md               # AI agent operating instructions
└── requirements.txt        # Python dependencies
```

## Architecture: The DOE Framework

This project follows a 3-layer architecture called **DOE** (Directive → Orchestration → Execution):

1. **Directives** — Markdown SOPs that define *what* to do, including edge cases and quality gates
2. **Orchestration** — An AI agent that reads directives, makes decisions, and calls scripts in the right order
3. **Execution** — Deterministic Python scripts that do the actual work (API calls, PDF generation, sheet writes)

This separation exists because LLMs are unreliable for deterministic tasks. By pushing all business logic into tested Python scripts, the AI only handles what it's good at: judgment calls, language generation, and error recovery.

## Setup

1. Clone the repo and install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy and fill in your profile:
   ```bash
   cp ABOUTME.example.md ABOUTME.md
   # Edit ABOUTME.md with your real data
   ```

3. Set up API keys in `.env`:
   ```
   OPENROUTER_API_KEY=...
   GOOGLE_AI_API_KEY=...
   SPREADSHEET_ID=...
   ```

4. Set up Google OAuth (`credentials.json`) for Sheets + Gmail access

5. Run individual stages:
   ```bash
   python execution/scrape_jobs.py
   python execution/evaluate_jobs.py
   python execution/write_jobs_to_sheet.py
   python execution/generate_cover_letter.py
   python execution/generate_cv.py
   python execution/send_followups.py --days 5  # dry run by default
   ```

---

<a id="deutsch"></a>

# DOE AI Agent — Automatisierte Bewerbungspipeline

> Ein KI-gestütztes System, das Stellenangebote automatisch sucht, bewertet und Bewerbungsunterlagen erstellt — entwickelt als Praxisbeispiel für die Automatisierung von Sales-Operations-Workflows.

## Das Problem

Jobsuche in der Schweiz bedeutet stundenlange Routinearbeit: Stellenbörsen durchsuchen, Beschreibungen lesen, passende Stellen identifizieren, Motivationsschreiben in der richtigen Sprache verfassen, den Lebenslauf pro Stelle anpassen und nach Wochen nachfassen. Das meiste davon ist manuell, langsam und fehleranfällig.

## Die Lösung

Dieses Projekt automatisiert den gesamten Bewerbungsprozess — vom Scraping über 29 Suchbegriffe auf mehreren Stellenbörsen bis hin zur Erstellung personalisierter, sprachlich abgestimmter Motivationsschreiben und Lebensläufe. Alles wird in einem Google Sheet nachverfolgt, und zeitgesteuerte Follow-up-E-Mails werden automatisch vorbereitet.

Das System verarbeitet **über 1'000 Stellenangebote pro Durchlauf**, bewertet jedes Angebot anhand eines persönlichen Profils und erstellt Bewerbungsunterlagen für die besten Treffer — ohne manuellen Aufwand.

## So funktioniert es

```
┌─────────────────────────────────────────────────────────────┐
│                    6-STUFEN-PIPELINE                         │
│                                                             │
│  ┌──────────┐   ┌──────────┐   ┌──────────────────────┐    │
│  │ 1. SUCHE │──▶│2. BEWERTEN│──▶│ 3. GOOGLE SHEET     │    │
│  │Stellenbörsen│ │ mit LLM  │   │ (Tracking)           │    │
│  └──────────┘   └──────────┘   └──────────────────────┘    │
│       │                                    │                │
│       │         ┌──────────┐   ┌──────────────────────┐    │
│       │         │5. LEBENS-│◀──│ 4. MOTIVATIONS-      │    │
│       │         │  LAUF    │   │    SCHREIBEN          │    │
│       │         │(PDF+DOCX)│   │ (PDF+DOCX)           │    │
│       │         └──────────┘   └──────────────────────┘    │
│       │                │                                    │
│       │         ┌──────────────────────┐                   │
│       │         │ 6. FOLLOW-UP-EMAILS  │                   │
│       │         │(nach N Arbeitstagen) │                   │
│       │         └──────────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

### Stufe 1 — Suche
Durchsucht mehrere Stellenbörsen (Indeed, Glassdoor, LinkedIn usw.) mit 29 gezielten Suchbegriffen. Entfernt Duplikate, normalisiert Daten und holt fehlende Stellenbeschreibungen direkt von Arbeitgeber-Websites.

### Stufe 2 — Bewertung
Ein LLM bewertet jede Stelle (0–100) anhand der Übereinstimmung mit dem Kandidatenprofil — unter Berücksichtigung von Fähigkeiten, Erfahrung, Standort und Sprache. Stellen unter dem Schwellenwert werden aussortiert.

### Stufe 3 — Google Sheet
Bewertete Stellen werden in ein Google Sheet geschrieben mit vollständigen Tracking-Spalten: Score, Job-ID, Titel, Unternehmen, Standort, Status, Kontaktdaten, Follow-up-Daten. Das Sheet ist append-only mit URL-basierter Deduplizierung.

### Stufe 4 — Motivationsschreiben
Für jede Top-Stelle wird ein massgeschneidertes Motivationsschreiben in der erkannten Sprache (Deutsch, Englisch oder Italienisch) erstellt. Qualitätskontrollen stellen sicher, dass Wortanzahl, Ton und Formatierung den Schweizer Marktstandards entsprechen.

### Stufe 5 — Lebenslauf
Ein passender Lebenslauf wird pro Bewerbung erstellt, wobei die Fähigkeiten nach Relevanz für die jeweilige Stelle sortiert werden. Die Sprache bleibt konsistent mit dem Motivationsschreiben.

### Stufe 6 — Follow-up-E-Mails
Nach einer konfigurierbaren Anzahl Arbeitstage werden personalisierte Follow-up-E-Mails erstellt und versendet. Standardmässig im Dry-Run-Modus — nichts wird ohne explizite Bestätigung gesendet.

## Zentrale Designentscheidungen

| Entscheidung | Warum |
|-------------|-------|
| **Deterministische Skripte statt KI für alles** | LLMs sind probabilistisch. Geschäftslogik (Deduplizierung, Formatierung, Sheet-Schreibvorgänge) muss zuverlässig sein. KI wird nur dort eingesetzt, wo Urteilsvermögen gefragt ist. |
| **Dreisprachig (DE/EN/IT)** | Der Schweizer Arbeitsmarkt erfordert Deutsch, Englisch und Italienisch. Die Sprache wird pro Stellenangebot automatisch erkannt. |
| **Alles mit Checkpoints** | Jede Stufe speichert den Fortschritt. Ein Absturz bei Stelle Nr. 150 verarbeitet die ersten 149 nicht erneut. |
| **Primärschlüssel-System (Job-ID)** | Deterministischer `J-XXXXXX`-Hash verknüpft Sheet-Zeilen, Bewerbungsordner und Checkpoints — kein manuelles Zuordnen nötig. |
| **Datenschutzbewusstes LLM-Routing** | Persönliche Daten werden nur an vertrauenswürdige Anbieter gesendet. Externe Anbieter erhalten ein anonymisiertes Profil. |
| **Dry-Run als Standard** | Follow-up-E-Mails, Sheet-Löschungen und Migrationen sind standardmässig im Testmodus. Destruktive Aktionen erfordern explizite Flags. |

## Technologie

- **Python 3.11+** — Alle Pipeline-Skripte
- **LLMs** — Qwen3-235B (via OpenRouter) + Gemini Flash (Google AI Studio) als Fallback
- **Google Sheets API** — Bewerbungstracking und Statusverwaltung
- **Gmail API** — Follow-up-E-Mail-Versand
- **python-jobspy** — Multi-Plattform Job-Scraping
- **ReportLab** — PDF-Erstellung für Lebensläufe und Motivationsschreiben
- **python-docx** — DOCX-Erstellung

---

Built by [simon-per](https://github.com/simon-per) as a practical demonstration of AI-augmented workflow automation.
