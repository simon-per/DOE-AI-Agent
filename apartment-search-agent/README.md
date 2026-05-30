# Apartment Search Agent

Purpose-built workspace for Simon's WG/apartment search near Root D4, optimized for commute by public transport and e-bike.

## Current Strategy

The highest ROI is short commute to PHENOGY AG at Root D4. The system should not simply minimize rent; it should optimize effective life quality and earning capacity under the CHF 1000 ceiling.

Primary workflow:

1. Collect listings from saved searches and alerts.
2. Normalize listing data into one tracker.
3. Dedupe aggregator/original duplicates.
4. Filter hard exclusions.
5. Score commute and WG fit.
6. Generate a tailored application draft.
7. Simon approves and sends.

## Initial Platforms

High priority:
- Flatfox
- wgzimmer.ch
- weegee.ch
- StuWo
- Easy-Living
- wgstube

Broader property portals:
- Homegate
- ImmoScout24
- Newhome
- Urbanhome
- Comparis

Private/sublet sources:
- tutti.ch
- anibis.ch
- Ronorp
- Facebook groups, manual and scam-filtered

## End-To-End Workflow

Use the workflow runner for the normal apartment search loop:

```powershell
python execution\apartment_workflow.py --dry-run --flatfox-max-pages 3
python execution\apartment_workflow.py --flatfox-max-pages 25 --flatfox-sleep-seconds 2 --create-sheet-if-missing
```

The workflow runner:

- ingests Flatfox via the documented public API unless `--skip-flatfox` is set
- ingests saved-search alert emails (WGZimmer / Homegate / Newhome /
  ImmoScout24 / Flatfox) over Gmail IMAP unless `--skip-emails` is set —
  this is the compliant alternative to scraping bot-protected portals, see
  `directives/ingest_email_alerts.md`
- scores, dedupes, and generates German drafts in SQLite
- exports `data/application_queue.csv` and `data/message_drafts.md`
- syncs the Google Sheet review tabs
- shows a compliant daily application plan
- never sends applications or portal messages

Dry-run mode copies the current tracker to `.tmp/`, performs the same workflow
against temporary files, and runs Google Sheets in dry-run mode. It does not
modify the real tracker, exports, or Google Sheet.

## Manual Local Tools

Use the deterministic tracker before adding any account automation:

```powershell
python execution/apartment_pipeline.py init-db
python execution/apartment_pipeline.py ingest --url "https://example.ch/listing/123" --text-file .tmp/listing.txt
python execution/apartment_pipeline.py queue
python execution/apartment_pipeline.py daily-plan --daily-limit 10 --site-daily-limit 3
python execution/apartment_pipeline.py export
```

For multiple listings at once (Facebook groups, private leads, any portal
without an alert path), use the batch CLI — paste blocks separated by `===`:

```powershell
python execution/manual_paste.py --file .tmp/batch.txt --verbose
type .tmp/batch.txt | python execution/manual_paste.py --dry-run
```

Block format is documented in `execution/manual_paste.py`'s docstring.

## Live Commute Scoring

The pipeline scores commute by the **faster of two live paths**, falling back
to a hand-curated city → minutes table when neither is reachable — listings
never break for lack of routing. Both results cache in
`data/commute_cache.sqlite`, so each address hits an API at most once.

- **E-bike** — OpenRouteService routing to PHENOGY's coordinates. Opt-in: set
  `OPENROUTESERVICE_API_KEY` in `.env` (2,000 free requests/day).
- **Public transport (oeV)** — transport.opendata.ch, **no key required, on by
  default**. This is what lifts Lucerne-core listings from B to A: ~11 min by
  train vs ~35 min by bike. Set `TRANSPORT_OPENDATA_ENABLED=0` to disable.

```powershell
python execution/commute_scoring.py "Buchrain, Switzerland"
# -> 'Buchrain, Switzerland': 22 min via e-bike (live) (6.4 km)
python execution/transit_scoring.py "Luzern" --to "Root D4"
# -> 'Luzern' -> 'Root D4': 11 min via oeV (live)
```

When both resolve, the tracker shows both legs (e.g. `oeV 11 / e-bike 35
(live)`) and ranks on the minimum. If an API is missing, rate-limited, or down,
that leg is silently skipped.

## New-Listing Notifications

A+/A listings are invisible until you open the Google Sheet. `execution/listing_notifier.py`
bundles new `decision=apply` rows into a single Gmail message so strong matches
reach you immediately. Each listing is a complete **action packet** — both
commute legs, WG-fit, rent verdict, the direct apply link (and contact email
when known), and the ready-to-send German draft — so you can act from your phone
without opening the Sheet. The email is sent as text + HTML (tappable links,
copy-friendly draft block). It reuses the parent project's `gmail_send` helper
(Gmail app password) and stamps each notified row so it is never re-sent.

```powershell
python execution/listing_notifier.py --dry-run --preview --since 7d  # see the brief
python execution/listing_notifier.py --send --since 2d               # actually email
```

The full workflow runs it automatically after scoring (disable with
`--skip-notify`, defaults to a 2-day look-back via `--notify-since`). Set
`APARTMENT_NOTIFY_TO` to send somewhere other than your own `GMAIL_ADDRESS`.
DRY RUN by default — it only emails with `--send` or from a live (non-dry-run)
workflow.

The script creates/updates `data/listings.sqlite`, dedupes listings, scores commute/rent/WG fit, flags exclusions and scam risk, and writes:

- `data/application_queue.csv`
- `data/message_drafts.md`

It does not scrape websites and does not send applications. Use `approve` only after Simon approves a listing, then `mark-sent` after Simon manually sends it.

Safety details:

- approvals are reset if a listing changes after approval
- `mark-sent` refuses unapproved, changed, skipped, gender-excluded, or high-scam-risk listings unless forced for historical logging
- per-site daily planning uses a rolling 24-hour window
- generated tracker/draft files under `data/` are ignored by git

## Flatfox Public API

The first source integration uses Flatfox's documented public listing endpoint:

```powershell
python execution\flatfox_public_sync.py --max-pages 3 --sleep-seconds 1 --verbose
python execution\apartment_pipeline.py queue
python execution\apartment_pipeline.py export
```

For a broader but still rate-limited crawl of active public listings, use a
moderate page cap and rerun later rather than hammering the API:

```powershell
python execution\flatfox_public_sync.py --max-pages 25 --sleep-seconds 2
```

The Flatfox sync:

- calls only `GET /api/v1/public-listing/`
- uses no login and no API key for public listings
- filters locally for target cities, the Luzern-Rotkreuz coordinate corridor, rent, and apartment/WG object types
- uses `--area luzern-rotkreuz --corridor-km 5` by default
- supports `--bbox SOUTH WEST NORTH EAST` for a custom local coordinate box
- does not reduce Flatfox pagination unless a documented `--selection` ID is provided
- accepts only monthly Flatfox prices for rent scoring
- can pass a documented Flatfox `selection` ID via `--selection` if we obtain one later
- writes matches into the same SQLite tracker
- does not create contact requests, submit applications, or send messages
- leaves approval and send logging in `apartment_pipeline.py`

Because Flatfox's documented public endpoint does not expose direct public
price/category/geo query parameters, local filtering can only guarantee full
coverage after a broad, throttled crawl or when using a valid Flatfox
`selection` ID.

## Google Sheets Tracker

SQLite remains the source of truth, but the tracker can be mirrored to Google
Sheets for review:

```powershell
python execution\google_sheets_sync.py --dry-run
python execution\google_sheets_sync.py --sheet-name "Swiss Appartment Search Pipeline"
```

By default the child repo reads the parent DOE AI Agent OAuth credentials and
keeps the writable token local to this repository:

```text
GOOGLE_SHEET_NAME=Swiss Appartment Search Pipeline
GOOGLE_CREDENTIALS_PATH=../credentials.json
GOOGLE_TOKEN_PATH=token.json
GOOGLE_PARENT_TOKEN_PATH=../token.json
```

If local `token.json` is missing, the script uses `../token.json` as a
read-only seed and then stores any refreshed token in this repo. A legacy
`GOOGLE_TOKEN_PATH=../token.json` value is also treated as a read-only seed; the
script will not write token refreshes outside this repository. The sync opens
the existing spreadsheet by name and creates/updates these tabs:

- `Pipeline` - one row per listing, including status, score, listing facts, draft, and application fields
- `Summary`
- `Settings`
- `Sources`

If the OAuth token cannot see the named spreadsheet yet, create it explicitly:

```powershell
python execution\google_sheets_sync.py --sheet-name "Swiss Appartment Search Pipeline" --create-if-missing
```

Optional legacy service-account fallback is still available with
`GOOGLE_SHEETS_CREDENTIALS_PATH` plus `GOOGLE_SHEET_ID`, but OAuth is the normal
path for this workspace.

Dependencies expected in the Python environment:

- `python-dotenv`
- `gspread`
- `google-auth`
- `google-auth-oauthlib`
- `google-api-python-client` for the optional service-account fallback

The `Listings` tab includes empty `openrouter_score` and `openrouter_reason`
columns. Do not add paid OpenRouter scoring until Simon explicitly approves the
model, budget, and scoring prompt.

The sync owns generated columns and clears stale generated rows/columns inside
the worksheet grid. The `Pipeline` tab is keyed by `listing_id`: generated
listing context is refreshed, while manual columns such as `status`,
`final_message`, `sent_at`, `follow_up_date`, and `response_notes` are preserved
on each sync. The old generated `Listings`, `Queue`, and `Applications` tabs are
migrated into `Pipeline` and removed after sync. A blank default
`Sheet1`/`Tabellenblatt1` tab is removed after sync, and generated tabs are
ordered as `Pipeline`, `Summary`, `Settings`, `Sources`.

## Setup Notes

Create accounts and saved searches manually on each portal. Prefer email alerts as the ingestion source. Avoid account automation unless a site provides an official API or Simon explicitly approves a narrow manual-assist workflow.

Useful future integrations:
- Gmail or Outlook OAuth for alert ingestion
- Google Sheets API for the live tracker
- OpenRouteService API for e-bike scoring
- Swiss public transport API / OpenTransportData for public transport commute scoring

## Workspace Layout

```text
directives/   SOPs for search, scoring, applications, safety
execution/    deterministic scripts, added only after directive is clear
data/         persistent local tracker database or exports
.tmp/         regenerated intermediates, ignored by git
```
