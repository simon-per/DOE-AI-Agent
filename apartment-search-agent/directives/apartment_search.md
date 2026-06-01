# Apartment Search Directive

## Objective

Find WG rooms or small apartments near Root D4 that are affordable, realistic, and strong for Simon's daily commute.

## Search Criteria

Required:
- available around mid July
- rent below CHF 1000
- suitable for a male applicant
- plausible viewing/application process
- acceptable Anmeldung unless explicitly treated as temporary housing

Preferred:
- below CHF 800
- A+ or A commute to PHENOGY Root D4
- furnished or easy move-in
- calm and clean WG
- young professionals or mixed WG
- secure e-bike storage

## Priority Locations

Primary:
- Root D4
- Root
- Gisikon-Root
- Honau
- Dierikon
- Buchrain
- Ebikon
- Rotkreuz

Secondary:
- Luzern when the connection is fast and simple
- Perlen
- Inwil
- Adligenswil
- Emmen / Emmenbruecke

## Sources

Start with:
- Flatfox
- wgzimmer.ch
- weegee.ch
- StuWo
- Easy-Living
- wgstube
- Homegate
- ImmoScout24
- Newhome
- Urbanhome
- Comparis
- tutti.ch
- anibis.ch
- Ronorp
- Facebook groups, manual review only

## Workflow

1. Ingest saved-search alerts or manually provided listing URLs.
2. Reconcile liveness of previously-ingested Flatfox listings (per-pk public-API
   check); mark taken-down ones `expired` so they drop out of the queue, brief,
   plan, re-rank, and Sheet. Fail-safe: never expire on an API/network error or
   an ambiguous (pk-filter-ignored) response. Bounded by `--reconcile-stale-hours`
   / `--reconcile-max-checks`; `--skip-reconcile` opts out.
3. Normalize listing fields.
4. Dedupe the listing against existing tracker entries.
5. Apply hard filters.
6. Score commute.
7. Score WG fit.
8. Generate a tailored application draft.
9. Present the top queue to Simon.
10. Send only after approval or prepare a draft for Simon to send.

## Current Deterministic Tools

Use `execution/apartment_workflow.py` for the normal end-to-end loop:

```powershell
python execution\apartment_workflow.py --dry-run --flatfox-max-pages 3
python execution\apartment_workflow.py --flatfox-max-pages 25 --flatfox-sleep-seconds 2 --create-sheet-if-missing
```

The workflow runner may fetch Flatfox through the documented public API, update
the local tracker, export drafts, sync Google Sheets, and print a safe daily
application plan. It must not send applications, create portal contact requests,
or bypass bot protections. Use `--dry-run` first after changing workflow code;
dry-run uses `.tmp/` copies and Google Sheets dry-run mode.

Use `execution/apartment_pipeline.py` for the first local implementation.

Recommended commands:

```powershell
python execution/apartment_pipeline.py init-db
python execution/apartment_pipeline.py ingest --url "<listing-url>" --text-file .tmp/listing.txt
python execution/apartment_pipeline.py queue
python execution/apartment_pipeline.py daily-plan --daily-limit 10 --site-daily-limit 3
python execution/apartment_pipeline.py export
```

The tool stores listings in `data/listings.sqlite`, exports the active queue to
`data/application_queue.csv`, and exports German drafts to
`data/message_drafts.md`.

Use `execution/google_sheets_sync.py` to mirror the local tracker into Google
Sheets after ingestion/scoring:

```powershell
python execution\google_sheets_sync.py --dry-run
python execution\google_sheets_sync.py --sheet-name "Swiss Appartment Search Pipeline"
```

Google Sheets is a review surface, not the source of truth. SQLite remains the
canonical tracker. The generated `Pipeline`, `Sources`, `Summary`, and
`Settings` tabs may be overwritten by sync. `Pipeline` is the primary fact table:
one row per `listing_id`, with listing facts, score/ranking fields, a simple
manual `status`, generated draft text, and manual application notes. Preserve
manual columns such as `status`, `simon_approval`, `final_message`, `sent_at`,
`follow_up_date`, and `response_notes` by `listing_id`. The sync may remove old
generated `Listings`, `Queue`, and `Applications` tabs after migrating declared
manual fields into `Pipeline`. The sync should remove a blank default
`Sheet1`/`Tabellenblatt1` tab and order generated tabs as `Pipeline`, `Summary`,
`Settings`, `Sources`, so Simon does not open the document on an empty default
tab.

`Pipeline` is the masterfile: it shows only relevant listings (decision
`apply`/`consider`/`manual_review`; system `skip`/`expired` rows are hidden) with
the glanceable columns first and a job-style manual `Status` (New, Applied,
Interviewing, Offer, Rejected, Irrelevant, Duplicate, Expired, No_Response). On a
real (non-dry-run) sync the tab is also formatted via the Sheets `batchUpdate`
API: a frozen bold header, a Status dropdown (data validation), per-status colors,
and green/yellow/red score bands. Formatting is wrapped so it can never abort the
value sync, but a failure now prints a loud `!!! Pipeline formatting FAILED` banner
plus a full traceback — never a silent one-liner. Learnings: (1) formatting only
lands on a completed live sync, never on `--dry-run`; (2) idempotency depends on
fetching `conditionalFormats` explicitly via a `fields` mask — a bare
`fetch_sheet_metadata` may omit it, which makes the delete-then-re-add cleanup
skip its deletes and pile up duplicate color rules across daily re-syncs.

The child repo should read the parent DOE AI Agent Google OAuth credentials by
default, but keep writable token refreshes inside this repository:
- credentials seed: `../credentials.json`
- repo-local token: `token.json`
- parent token seed: `../token.json`
If an older `.env` still sets `GOOGLE_TOKEN_PATH=../token.json`, the sync script
must treat that file as a read-only parent seed and write refreshed tokens only
to repo-local `token.json`. Before reusing an OAuth token, validate the scopes
recorded in the token JSON itself; do not rely only on `google-auth`
`has_scopes()` after loading requested scopes.

Open the existing spreadsheet by name unless a specific `GOOGLE_SHEET_ID` is
provided. Service-account auth is only a legacy fallback for cases where
`GOOGLE_SHEETS_CREDENTIALS_PATH` and `GOOGLE_SHEET_ID` are both configured.
If the named sheet is not visible to the parent OAuth token, rerun with
`--create-if-missing` to create the tracker under that OAuth app instead of
switching to scraping or unsupported automation.

Important current boundary:
- the tool does not scrape portals
- the tool does not bypass CAPTCHAs or anti-bot systems
- the tool does not send messages
- `approve` records Simon's approval
- `mark-sent` is only for logging after Simon has manually sent or explicitly approved sending

## Flatfox Public API Ingestion

Flatfox's API documentation exposes a public listing endpoint:

```text
GET /api/v1/public-listing/
```

The public-listing docs state that this endpoint lists currently published
Flatfox listings and does not require an API key. The full Swagger schema shows
documented query parameters including `limit`, `offset`, `pk`, `project`,
`organization`, `organization__slug`, `selection`, `status`, `expand`, and
`include`.

Use `execution/flatfox_public_sync.py` as the Flatfox source adapter:

```powershell
python execution\flatfox_public_sync.py --max-pages 3 --sleep-seconds 1 --verbose
python execution\apartment_pipeline.py queue
```

Operational rules:
- only call documented public-listing endpoints unless this directive is extended
- keep pagination capped and rate-limited
- filter locally by target city, rent ceiling, and apartment/WG object type
- use the local Luzern-Rotkreuz corridor by default (`--area luzern-rotkreuz --corridor-km 5`) so listings between named cities can match by coordinates
- use `--bbox SOUTH WEST NORTH EAST` only for explicit local coordinate experiments
- remember that local area/category/price filters do not reduce Flatfox pagination; full coverage needs a broad throttled crawl or a valid documented `selection` ID
- pass `--selection <id>` only when a valid documented Flatfox selection ID is available
- use only monthly Flatfox prices for budget scoring; non-monthly prices require a future explicit conversion/review rule
- store Flatfox public API matches under source `flatfox.ch` so same-site application caps still apply
- never use this source adapter to submit applications or contact requests
- route all matching listings through the local tracker, dedupe, scoring, draft generation, and approval flow

## Hard Exclusions

Skip listings that say:
- nur Frauen
- only women
- Frauen-WG only
- weibliche Mitbewohnerin only
- no Anmeldung, unless temporary fallback
- deposit or payment before viewing/contract
- suspicious remote landlord/key-shipping setup

For "women preferred" or softer wording, do not auto-send. Mark for manual review.
