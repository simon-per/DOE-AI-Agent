# Workspace Handoff

Open this folder as its own VS Code workspace:

```text
C:\Users\simon\Desktop\DOE AI Agent\apartment-search-agent
```

The workspace is intentionally separated from the DOE agent to avoid context pollution.

## What Was Carried Over

The previous apartment-search discussion has been condensed into:

- `AGENTS.md`: operating rules and project-level instructions
- `ABOUTME.md`: Simon profile used for application drafts
- `directives/apartment_search.md`: search workflow
- `directives/listing_scoring.md`: commute and listing scoring
- `directives/application_messages.md`: German WG message templates
- `directives/anti_ban_rules.md`: compliance and anti-ban rules
- `CONNECTIONS.md`: setup checklist for accounts, alerts, and APIs

## Next Build Step

The first implementation should probably be:

1. create a `listings` tracker schema
2. implement manual URL/listing entry
3. implement commute scoring
4. implement message draft generation
5. only then add email alert ingestion

This order gets useful results before touching accounts or automation-sensitive flows.

## Initial Success Criteria

Given a listing URL or copied listing text, the workflow should output:

- keep/skip decision
- commute class A+/A/B/C
- scam/gender restriction flags
- concise reason
- tailored German application draft
- recommended send priority

