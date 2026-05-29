# Agent Instructions

This workspace is for Simon's Swiss apartment and WG search around Root D4 / Luzern.

## Goal

Find and apply to high-quality WG rooms or small apartments near PHENOGY AG, Platz 4, 6039 Root D4, with a strong preference for short commute by public transport or e-bike.

Primary move-in target: mid July, around 16.07.

Budget:
- Ideal: below CHF 800
- Acceptable: below CHF 1000 when commute and WG fit are strong

## Operating Model

Use a 3-layer architecture:

1. Directives in `directives/` define the SOPs.
2. Codex does orchestration, judgment, triage, and final approval routing.
3. Deterministic scripts in `execution/` handle repeatable work such as email ingestion, deduping, commute scoring, and draft generation.

Before writing new scripts, check `execution/` for existing tools.

After creating or modifying any script in `execution/`, review it before considering the task complete. If a reviewer sub-agent is available, use it with `directives/review_execution_script.md`; otherwise do a documented manual review and note the limitation.

## Safety And Anti-Ban Rules

Do not auto-submit portal applications unless Simon explicitly approves each listing.

Do not:
- bypass CAPTCHAs or bot protections
- use stealth scraping services
- use reverse-engineered private APIs
- scrape behind login when saved searches, email alerts, or official flows are sufficient
- send identical mass messages
- apply to listings that explicitly say only women / nur Frauen / only female tenants
- duplicate-apply to the same listing through both an aggregator and original source

Prefer:
- saved search alerts
- official notifications
- public pages where allowed
- email drafts
- user-approved portal messages
- low-volume, personalized outreach

Default daily application volume: 5-15 strong listings, unless Simon explicitly changes this.

## Search Target

Optimize around:

PHENOGY AG
Platz 4
6039 Root D4
Switzerland

Priority areas:
- Root D4
- Root
- Gisikon-Root
- Honau
- Dierikon
- Buchrain
- Ebikon
- Rotkreuz
- Luzern, only when door-to-door commute is strong

Secondary areas:
- Perlen
- Inwil
- Adligenswil
- Emmen / Emmenbruecke
- Kriens only if commute and price are unusually good

## Ranking

Commute has high ROI and should be weighted almost like rent.

A+:
- under about 20 minutes door-to-door to Root D4 by e-bike or public transport
- apply immediately if other constraints pass

A:
- 20-30 minutes reliable commute
- apply same day

B:
- 30-45 minutes
- apply only if cheap, strong WG fit, or temporary fallback

C:
- over 45 minutes
- skip unless emergency or unusually strong

Use the time-value logic:

15 minutes saved each way = about 30 minutes per workday = about 10 hours per month. A more expensive room can be rational if it saves meaningful daily commute time.

## Personal Positioning

Simon is:
- 23 years old
- male
- starting at PHENOGY in Root D4 on 16.07
- from the BESS / renewable energy space
- using public transport and e-bike
- looking for a WG-compatible, calm, clean, reliable living situation
- social and relaxed, but not a party-animal

Application tone should feel like a Swiss WG application: personal, warm, reliable, and concise.

## Deliverables

Local files are for processing. Main deliverables should be accessible and actionable:
- ranked listing tracker
- application queue
- message drafts
- application status log
- follow-up reminders

Use `.tmp/` for intermediate exports and scraped/transient data. Do not commit `.tmp/`.

