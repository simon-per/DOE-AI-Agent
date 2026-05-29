# Connections Setup

This file tracks what Simon needs to connect or create for the apartment-search workflow.

Do not store passwords here. Use `.env` for API keys and OAuth client IDs, and keep `.env` uncommitted.

## Portal Accounts

Create or verify accounts:

- [ ] Flatfox
- [ ] wgzimmer.ch
- [ ] weegee.ch
- [ ] Homegate
- [ ] ImmoScout24
- [ ] Newhome
- [ ] Urbanhome
- [ ] Comparis
- [ ] tutti.ch
- [ ] anibis.ch
- [ ] Ronorp
- [ ] Facebook account/groups, manual workflow only

## Flatfox API

Current implementation:

- [x] Public listing ingestion via documented `GET /api/v1/public-listing/`
- [x] No API key required for public listings
- [x] Local filtering/deduping/scoring before drafts
- [ ] Authenticated Flatfox API key, only if needed later for approved contact/application workflows

Do not use authenticated contact-request or application endpoints until the
approval flow and required applicant/profile fields are explicitly designed.

## Saved Searches

Create saved searches for:

- Root D4
- Root
- Gisikon-Root
- Honau
- Dierikon
- Buchrain
- Ebikon
- Rotkreuz
- Luzern with strong connection to Root D4

Rent:
- maximum CHF 1000
- optionally separate alert for maximum CHF 800

Object types:
- WG room
- furnished room
- studio / 1-room apartment
- temporary room if Anmeldung and contract are acceptable

## Email Alert Ingestion

Preferred source for automation: saved-search email alerts. This is the
**compliant alternative to scraping** the four major Swiss portals — all of
them (Homegate, Newhome, ImmoScout24, WGZimmer) return HTTP 403 to direct
requests, so we ingest the alert emails they send instead.

Current setup:

- [x] Gmail IMAP via app password (`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD`
      inherited from parent `../.env`)
- [x] Read-only — `select(..., readonly=True)`; we never STORE flags or
      move messages on the server
- [x] Local dedupe in `email_alert_seen` SQLite table keyed on `Message-ID`
- [x] Parser registry covers WGZimmer / Homegate / Newhome / ImmoScout24 /
      Flatfox; unknown senders dump to `.tmp/email_samples/`
- [x] `directives/ingest_email_alerts.md` documents the SOP

Saved-search setup is per-portal (do once):

- [ ] WGZimmer.ch — "Suchabo" for Luzern + Zug, ≤ CHF 1000
- [ ] Homegate.ch — "Suchabo" for Luzern, ≤ CHF 1000, Wohnung/Studio/WG-Zimmer
- [ ] Newhome.ch — "Suchauftrag" with same filters
- [ ] ImmoScout24.ch — "Suchauftrag" Luzern/Zug ≤ CHF 1000

Commands:

```powershell
python execution\email_alert_ingest.py --days 30 --dry-run --verbose
python execution\email_alert_ingest.py
python execution\email_alert_ingest.py --reprocess --days 90 --verbose
```

Outlook OAuth path is reserved but not implemented — IMAP + Gmail covers the
need today.

## Commute APIs

For e-bike routing:

- [ ] OpenRouteService API key

For public transport:

- [ ] transport.opendata.ch, no key for simple Transport API use
- [ ] OpenTransportData API key if using the official API manager / OJP flow later

Search target:

```text
PHENOGY AG
Platz 4
6039 Root D4
Switzerland
```

## Tracker

Current setup:

- [x] local SQLite database in `data/listings.sqlite`
- [x] CSV/Markdown exports
- [x] end-to-end local workflow runner
- [x] Google Sheets sync script
- [x] parent DOE AI Agent OAuth credentials read from `../credentials.json`
- [x] repo-local writable Google token at `token.json`, seeded from `../token.json` when needed
- [x] legacy `GOOGLE_TOKEN_PATH=../token.json` is treated read-only and not overwritten
- [x] target Google Sheet name: `Swiss Appartment Search Pipeline`
- [ ] optional Google Sheet ID configured only if opening by name is insufficient

SQLite remains the source of truth. Google Sheets is the readable review layer.
The main working tab is `Pipeline`: one row per listing with a simple manual
`status`, generated score/reason/draft fields, and application notes. Supporting
tabs are `Summary`, `Settings`, and `Sources`. The old generated `Listings`,
`Queue`, and `Applications` tabs are migrated into `Pipeline` and removed after
sync. Blank default `Sheet1`/`Tabellenblatt1` tabs are removed after sync so the
document opens on generated tracker tabs.

Commands:

```powershell
python execution\apartment_workflow.py --dry-run --flatfox-max-pages 3
python execution\apartment_workflow.py --flatfox-max-pages 25 --flatfox-sleep-seconds 2 --create-sheet-if-missing
python execution\google_sheets_sync.py --dry-run
python execution\google_sheets_sync.py --sheet-name "Swiss Appartment Search Pipeline"
python execution\google_sheets_sync.py --sheet-name "Swiss Appartment Search Pipeline" --create-if-missing
```

## Source Registry

Tracked in `data/source_registry.csv`.

Current source strategy:

- Flatfox: public API implemented + email alerts as backup
- WGZimmer / Homegate / Newhome / ImmoScout24: email-alert ingestion implemented
  (`execution/email_alert_ingest.py`). User sets up saved searches in each
  portal UI; the ingester pulls them from Gmail IMAP.
- Facebook/private sources: manual review only
- authenticated sending/contact workflows: not implemented until approval flow is extended

## OpenRouter Scoring

The Google Sheet has placeholder columns:

- `openrouter_score`
- `openrouter_reason`

Do not run paid LLM scoring until Simon approves the model, budget, and exact scoring prompt.

## Sending Policy

Default:

- Generate drafts automatically.
- Simon approves each application.
- Send manually or semi-manually.

Email can be automated later only with explicit per-listing approval.

Portal messages should remain manual or browser-assisted unless the platform provides an official supported automation path.
