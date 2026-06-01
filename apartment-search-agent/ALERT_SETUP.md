# Turn on the portal alerts (one-time, ~20 min)

This is the single biggest lever for **more relevant listings**. Right now the
tracker only fills from Flatfox. The other 10 portals feed your sheet **only when
they email you new matches** — so you need to create one free saved-search alert per
portal. The agent reads those alert emails (read-only) and drops the listings into
your Google Sheet automatically.

## The rule for every portal

For each saved search, use the **same filters**:

- **Where:** Luzern (canton) — and add **Zug** where offered (covers Rotkreuz).
  If the portal asks for a town + radius, use **Root** (or **Ebikon**) **+ ~10 km**
  — that covers Root D4, Gisikon-Root, Honau, Dierikon, Buchrain, Ebikon, Rotkreuz,
  Perlen, Inwil, Adligenswil, Emmen, and Luzern.
- **Max rent:** **CHF 1000**
- **Type:** WG-Zimmer (room) **and** Wohnung / Studio (small apartment)
- **Notification:** **email**, instant or daily — **sent to `simonobemair@gmail.com`**

## ⚠️ One Gmail rule

The alert emails must land in your **Inbox**. Don't create a Gmail filter that
"Skip the Inbox (Archive it)" or files them under a label only — the agent scans
`INBOX`. (If you'd rather they go to a label, tell me the label name and I'll point
the ingest at it instead.)

## Per-portal (where to click)

| Portal | What to create | Notes |
|---|---|---|
| **WGZimmer.ch** | Log in → *Suchabo erstellen* | Canton Luzern (+ Zug); permanent + temporary |
| **Homegate.ch** | *Suchabo* from the search-results page | Wohnung + Studio + WG-Zimmer |
| **Newhome.ch** | *Suchauftrag* | Same filters; instant email is fine |
| **ImmoScout24.ch** | *Suchauftrag speichern* | Wohnung / Zimmer |
| **Comparis.ch** | *Suchabo* | Luzern + Zug, mieten (aggregator — extra coverage) |
| **Anibis.ch** | *Suchabo speichern* (apartment-rental category) | Catches private/small listings |
| **Tutti.ch** | *Suchauftrag* (immobilien, Luzern) | Often private one-room sublets |
| **Ronorp.net** | *Insert-Alarm* (Zentralschweiz → wohnen) | Strong for Luzern WG rooms |
| **WG-Gesucht.de** | *Suchauftrag* for Luzern (region 152), WG + 1-Zimmer | Net-new WG inventory |
| **UrbanHome.ch** | *Suchabo* (Luzern, mieten, Wohnung + WG) | Aggregator; daily email |
| **Flatfox.ch** | *(optional)* — already covered by the API | Alert only backs up the API |

## After you've set a few up

Tell me, and I'll run a **read-only** check that confirms the alert emails are
arriving and parsing correctly, then sync the new listings into your sheet. (A
couple of portals — UrbanHome, WG-Gesucht — have provisional link patterns I'll
finalize from the first real alert.)

Once alerts are flowing, the daily scheduled run (07:30) keeps the sheet fresh and
emails you a short digest of new strong matches — no action needed from you.
