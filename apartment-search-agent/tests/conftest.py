"""Test-suite defaults: keep unit tests offline and deterministic.

Listing scoring now consults two live commute paths — ORS e-bike
(commute_scoring) and transport.opendata.ch transit (transit_scoring). Any test
that scores a listing with a city would otherwise fire real HTTP. We force both
off here. Tests that exercise the live paths opt back in explicitly (by patching
the lookup, `api_key`, or `is_enabled`).

`commute_scoring._load_env_chain()` calls `load_dotenv(..., override=False)`, so
values we set here are NOT clobbered by a parent `.env` that happens to define an
ORS key or enable transit.
"""

import os

# Keyless transit is on by default in production — force it off for tests.
os.environ["TRANSPORT_OPENDATA_ENABLED"] = "0"
# Empty (not absent) so a parent .env can't re-enable the e-bike path via dotenv.
os.environ["OPENROUTESERVICE_API_KEY"] = ""
# Empty GMAIL creds so listing_notifier's send path is a guaranteed no-op even
# when the workflow runs notification live — no test ever sends a real email.
# (gmail_send loads the parent .env with override=False, so these "" win.)
os.environ["GMAIL_ADDRESS"] = ""
os.environ["GMAIL_APP_PASSWORD"] = ""
