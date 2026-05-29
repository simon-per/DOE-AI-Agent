# Ingest Email Alerts (Saved-Search Path)

## Goal

Pull new apartment listings into the local tracker from saved-search email
alerts on portals that block direct scraping. As of 2026-05-29 this covers
**WGZimmer, Homegate, Newhome, ImmoScout24, and Flatfox**.

This is the compliant alternative to scraping those portals — each one
returns HTTP 403 to direct requests, and `directives/anti_ban_rules.md`
forbids bypassing protections. Saved-search email alerts are free, official,
and don't require any portal-side automation.

## Inputs

- Simon's Gmail account, read via IMAP over SSL.
- `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` from the parent `.env`
  (already used by `../execution/gmail_send.py`). Falls back to the local
  `.env` if present.
- Saved-search alerts configured by Simon on each portal (see setup below).

## Tool

`execution/email_alert_ingest.py` — read-only IMAP fetcher with parser
dispatch. Never modifies labels or flags on the server. Tracks processed
messages in a local `email_alert_seen` SQLite table so reruns are idempotent.

```powershell
# Dry run: parse + score recent alerts, no DB writes
python execution\email_alert_ingest.py --days 30 --dry-run --verbose

# Real run: defaults are conservative (last 30 days, max 200 messages)
python execution\email_alert_ingest.py

# Replay everything in the window (ignore the seen-table)
python execution\email_alert_ingest.py --reprocess --days 90 --verbose
```

CLI flags:

| Flag | Default | Purpose |
|---|---|---|
| `--days` | 30 | IMAP `SINCE` window |
| `--limit` | 200 | Max messages per run |
| `--mailbox` | INBOX | IMAP mailbox to scan |
| `--reprocess` | off | Bypass the seen-table |
| `--dry-run` | off | No SQLite or seen-table writes |
| `--verbose` | off | Per-message log |

## Outputs

- Listings upserted via `apartment_pipeline.upsert_listing()` — same canonical
  key + content-hash dedupe as the Flatfox public path.
- `email_alert_seen(message_id, source, listings_created, processed_at)` row
  per processed message — prevents re-parsing on subsequent runs.
- Unknown-sender alerts dumped to `.tmp/email_samples/unrouted_*.eml` so the
  parser registry can be extended without re-running IMAP.

## Per-portal saved-search setup

Set up these searches once. They're free on every portal.

**WGZimmer.ch** — log in → "Suchabo erstellen" with:
- Canton: Luzern (+ optionally Zug)
- Max rent: 1000 CHF
- Permanent + temporary
- Notification: email, daily

**Homegate.ch** — "Suchabo" from the search results page:
- Canton: Luzern, optionally Zug
- Rent ≤ CHF 1000
- Wohnung + Studio + WG-Zimmer
- Notification: instant or daily

**Newhome.ch** — "Suchauftrag" with the same filters; instant email is fine.

**ImmoScout24.ch** — "Suchauftrag speichern":
- Canton: Luzern (+ Zug for commute corridor)
- Rent ≤ CHF 1000
- Wohnung / Zimmer
- Notification: instant

**Flatfox.ch** — public API path is primary, but alert subscriptions back
up the API in case it lags. No special filters needed.

## Parser registry

Defined in `execution/email_parsers/__init__.py`. Each parser:

1. Recognises its portal by sender domain (regex on `From:` header).
2. Extracts every URL matching the portal's listing-detail URL pattern.
3. Captures ~240 chars of context per URL — passed to the pipeline as
   `raw_text` so `extract_rent` / `extract_city` / `extract_move_in` can
   still populate fields downstream.

To add a new portal:

1. Create `execution/email_parsers/<portal>.py` with a subclass of
   `BaseEmailParser`. Set `source`, `sender_patterns`, `listing_url_pattern`.
2. Register the instance in `email_parsers/__init__.py::REGISTERED_PARSERS`.
3. Drop a representative `.eml` fixture in `tests/fixtures/email_alerts/`
   and add an assertion in `tests/test_email_parsers.py`.

## Edge cases and self-anneal notes

- Subject lines often have the only structured info we can extract reliably
  (rent + city). The base parser passes the subject as `raw_text` prefix.
- Some portals send digest emails with several listings. URL-pattern matching
  + `dedupe_urls` keeps the parser stateless.
- WGZimmer once sent a 1-listing email Simon already replied to — the
  send-confirmation. That listing still upserts cleanly (idempotent).
- If you discover an alert sender we don't recognise, the message gets
  dumped to `.tmp/email_samples/` instead of erroring. Iterate by reading
  the dump, updating the parser, rerunning with `--reprocess`.

## Safety boundaries

- **Read-only** — IMAP `select(..., readonly=True)`. We never `STORE` flags
  or move messages.
- **No sending** — this script never replies or contacts portals.
- **No password storage** — credentials live only in `.env` (parent first).
- **No bypass** — if a portal stops sending alerts, fix the search settings,
  do not start scraping the site.
