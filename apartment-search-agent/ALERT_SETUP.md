# Turn on the portal alerts (one-time, ~20 min) — without inbox spam

Your sheet currently fills only from Flatfox. The other 10 portals **block
scraping**, so their only compliant feed is their saved-search **email alerts**.
The trick to avoid inbox spam: route those alerts **straight to the archive** (skip
your inbox) with one Gmail filter. The agent harvests them from **All Mail** into
your sheet — so your inbox stays clean and the sheet still fills. Duplicates across
portals collapse automatically (dedup on listing URL + title/city/rent).

## Step 1 — Create a saved search + email alert on each portal

Same filters everywhere:

- **Where:** Luzern (canton) + add **Zug** where offered; or town **Root/Ebikon +
  ~10 km** (covers Root D4, Gisikon-Root, Honau, Dierikon, Buchrain, Ebikon,
  Rotkreuz, Perlen, Inwil, Adligenswil, Emmen, Luzern).
- **Max rent:** CHF 1000 · **Type:** WG-Zimmer + Wohnung/Studio
- **Notification:** email — pick **daily digest** where offered (fewer messages),
  sent to `simonobemair@gmail.com`.

| Portal | What to create | Notes |
|---|---|---|
| **WGZimmer.ch** | Log in → *Suchabo erstellen* | Canton Luzern (+ Zug); permanent + temporary |
| **Homegate.ch** | *Suchabo* from the search-results page | Wohnung + Studio + WG-Zimmer |
| **Newhome.ch** | *Suchauftrag* | Same filters |
| **ImmoScout24.ch** | *Suchauftrag speichern* | Wohnung / Zimmer |
| **Comparis.ch** | *Suchabo* | Luzern + Zug (aggregator — extra coverage) |
| **Anibis.ch** | *Suchabo speichern* (apartment-rental category) | Catches private/small listings |
| **Tutti.ch** | *Suchauftrag* (immobilien, Luzern) | Often private one-room sublets |
| **Ronorp.net** | *Insert-Alarm* (Zentralschweiz → wohnen) | Strong for Luzern WG rooms |
| **WG-Gesucht.de** | *Suchauftrag* for Luzern (region 152), WG + 1-Zimmer | Net-new WG inventory |
| **UrbanHome.ch** | *Suchabo* (Luzern, mieten, Wohnung + WG) | Aggregator |

## Step 2 — One Gmail filter so they never touch your inbox

1. Gmail → **Settings (gear) → See all settings → Filters and Blocked Addresses →
   Create a new filter**.
2. In the **From** field, paste:

   ```
   wgzimmer.ch OR homegate.ch OR immoscout24.ch OR newhome.ch OR comparis.ch OR anibis.ch OR tutti.ch OR ronorp.net OR wg-gesucht.de OR urbanhome.ch
   ```

3. Click **Create filter** (bottom-right), then tick:
   - ✅ **Skip the Inbox (Archive it)** — keeps your inbox clean
   - ✅ **Mark as read** — no unread badge anywhere
   - ☑ *(optional)* **Apply the label:** `Apartments` — so you can browse them if you like
   - ✅ **Also apply filter to matching conversations** (sweeps any already in your inbox)
4. Click **Create filter**.

Done. Alerts now bypass your inbox entirely; the daily run reads them from All Mail.

**Note — Flatfox is intentionally NOT in the list.** Flatfox emails are replies to
flats you've already contacted, so those stay in your inbox where you'll see them
(its listings come in via the API anyway). "Mark as read" does not hide alerts from
the agent — it reads All Mail by date + sender, not by read status.

## After you've set a few up

Just tell me. The daily run (07:30) harvests them into the sheet automatically — no
further action from you. A couple of portals (UrbanHome, WG-Gesucht) have
provisional link patterns; the system now auto-captures a sample of any alert it
can't parse yet, so I can finalize those quickly from the first real ones.
