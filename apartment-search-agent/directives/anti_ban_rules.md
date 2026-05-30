# Anti-Ban And Compliance Directive

## Principle

Speed should come from better triage and better drafts, not from spam behavior.

## Allowed Automation

Allowed:
- ingest alert emails
- parse public listing data from emails or manually supplied URLs
- dedupe listings
- score commute
- generate drafts
- prepare email drafts
- create a ranked queue
- remind Simon to follow up

Allowed with explicit per-listing approval:
- sending an email
- posting a portal message where a normal user would manually send it
- replying to WhatsApp or Facebook contact information

## Disallowed Automation

Do not:
- bypass CAPTCHA
- bypass paywalls or platform restrictions
- use stealth/proxy scraping
- reverse-engineer private mobile APIs
- scrape logged-in pages unless Simon explicitly approves a narrow action and the site's terms allow it
- send mass applications
- send duplicate applications to the same listing
- contact listings that explicitly exclude Simon

## Volume Limits

Default:
- 5-15 strong applications per day
- prioritize A+ and A commute listings
- do not send weak B/C applications just to increase volume
- while using `execution/apartment_pipeline.py`, use `daily-plan --daily-limit 10 --site-daily-limit 3` unless Simon explicitly changes the pace
- site limits are evaluated on a rolling 24-hour window, not a midnight reset
- `daily-plan` is a planning/export step only; it must not send or submit anything

## Current Bot-Safe Implementation Pattern

The initial automation boundary is:

1. Ingest manually supplied URLs, copied listing text, or saved-alert text.
2. Dedupe and score locally in SQLite.
3. Generate varied German drafts from listing-specific signals.
4. Present a queue for Simon's approval.
5. Log `approved` only after Simon approves the specific listing.
6. Log `sent` only after Simon manually sends it or explicitly authorizes sending.

Implementation notes for `execution/apartment_pipeline.py`:
- approvals are tied to the current listing content hash
- re-ingesting changed listing content resets approval before send logging
- URL query parameters are normalized and common tracking parameters are removed
- cross-source duplicate detection uses strict title/city/rent/move-in content keys when available

Implementation notes for `execution/flatfox_public_sync.py`:
- uses Flatfox's documented unauthenticated public listing endpoint only
- defaults to capped pagination and a one-second delay between pages
- handles Flatfox `429` rate-limit responses as a stop condition, not something to bypass
- treats all Flatfox listings as source `flatfox.ch` for same-site daily limits
- only monthly Flatfox prices are accepted for rent scoring
- performs local filtering instead of probing undocumented website search APIs
- imports matches into the tracker but never sends contact requests or application submissions
- authenticated Flatfox endpoints such as contact-request or application submission require a future directive update and explicit per-listing approval

Do not add portal sending, browser automation, or email sending to the execution
tool without extending this directive first and preserving explicit per-listing
approval.

## Paid LLM Scoring

OpenRouter paid LLM scoring is a second-pass qualitative rating that must remain
separate from the deterministic safety filters.

**Approved (Simon, 2026-05-30):** `execution/llm_rerank.py`, default model
`google/gemma-4-31b` (env-overridable via `MODEL_RERANK` / `OPENROUTER_MODEL`),
advisory only, manual opt-in. It runs dry-run by default (zero calls) and is
capped at `--max-rows` (default 40) per run; ~one short call per listing keeps
cost to a fraction of a cent. It is NOT part of the automated daily run.

Standing constraints (unchanged):
- the scoring prompt lives in the script config (`CRITERIA` in `llm_rerank.py`)
- the LLM score must not override hard exclusions
- the Google Sheet may display `openrouter_score` and `openrouter_reason`, but
  the deterministic `decision` and approval gates remain authoritative — the
  re-rank only re-orders / annotates within the `consider` tier

## Gender Restrictions

Hard skip:
- nur Frauen
- only women
- female only
- Frauen-WG only
- weibliche Mitbewohnerin gesucht, when phrased as a hard requirement

Manual review:
- Frauen bevorzugt
- ideally female
- preference language that is not a hard exclusion

## Scam Filters

Red flags:
- payment before viewing
- landlord abroad with key-shipping story
- rent far below market without explanation
- refusal to show the room
- copy-pasted contract images
- pressure to decide immediately
- request to buy furniture before application or viewing
- deposit to a private/non-Swiss account before contract clarity
