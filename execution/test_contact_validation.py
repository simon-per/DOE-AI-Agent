"""Pure-function unit tests for the contact-discovery validators.

Run as: `python execution/test_contact_validation.py`

Covers the three reliability layers added 2026-05-16 on top of the listing-
anchored waterfall:
  - _quote_in_text       (extract_contacts.py) — verifies LLM source_quote is grounded
  - _email_domain_acceptable / _build_expected_domains (discover_contacts.py)
  - _looks_like_aggregator (discover_contacts.py)

No mocks — all targets are pure functions over strings and dicts.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.extract_contacts import (
    _quote_in_text,
    _transliterate_to_ascii,
    _split_name_for_email,
    _infer_email_pattern,
    _construct_email,
)
from execution.discover_contacts import (
    _looks_like_aggregator,
    _domain_from_url,
    _email_domain_acceptable,
    _build_expected_domains,
    _resolve_company_domain_free,
    _rescue_pending_emails,
)
from execution.contact_scraper import _score_apply_link, _APPLY_MIN_SCORE


# ---------------------------------------------------------------------------
# Tiny test harness — keep this file standalone (no pytest dependency).
# ---------------------------------------------------------------------------

_failures: list[str] = []
_passes = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _passes
    if condition:
        _passes += 1
    else:
        _failures.append(f"  FAIL: {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# _quote_in_text
# ---------------------------------------------------------------------------

text_de = (
    "Bei Fragen zur Stelle wenden Sie sich bitte an Frau Anna Müller, "
    "HR Business Partner. Wir freuen uns auf Ihre Bewerbung."
)

check("quote: exact match",
      _quote_in_text("Frau Anna Müller, HR Business Partner", text_de))

check("quote: whitespace-only diff (newline vs space)",
      _quote_in_text("Frau Anna Müller,\nHR Business Partner", text_de))

check("quote: case diff",
      _quote_in_text("frau anna müller, hr business partner", text_de))

check("quote: tab + extra spaces normalize",
      _quote_in_text("Frau\tAnna  Müller,  HR\tBusiness Partner", text_de))

check("quote: paraphrase rejected",
      not _quote_in_text("Contact Anna Mueller from HR for any questions.", text_de))

check("quote: invented phrase rejected",
      not _quote_in_text("Send your CV to Maximilian Schmidt at HR.", text_de))

check("quote: empty quote rejected",
      not _quote_in_text("", text_de))

check("quote: empty text rejected",
      not _quote_in_text("Frau Müller", ""))

check("quote: both empty rejected",
      not _quote_in_text("", ""))

check("quote: whitespace-only quote rejected",
      not _quote_in_text("   \n\t ", text_de))

# Unicode look-alikes - LLMs tend to substitute smart quotes / en-dash / ellipsis
# even when instructed to copy verbatim. Use \u escapes so the test file itself
# stays trivially encoding-safe.
EN_DASH = "–"      # en-dash
LDQUO_DE = "„"     # German low-9 left double quote
RDQUO = "”"        # right double quote
ELLIPSIS = "…"     # horizontal ellipsis
NBSP = " "         # non-breaking space
UMLAUT_U = "ü"     # u with diaeresis

text_with_unicode = (
    "Bei Fragen wenden Sie sich bitte an Frau Anna M" + UMLAUT_U + "ller " + EN_DASH + " HR Business Partner. "
    + "Sie erreichen sie unter " + LDQUO_DE + "anna.mueller@example.ch" + RDQUO + "."
)
# LLM "quote" uses ASCII hyphen and ASCII straight quote -> should still match.
check("quote unicode: en-dash in text vs ASCII hyphen in quote",
      _quote_in_text("Frau Anna M" + UMLAUT_U + "ller - HR Business Partner", text_with_unicode))
check("quote unicode: smart-quote in text vs ASCII straight quote in quote",
      _quote_in_text("unter \"anna.mueller@example.ch\"", text_with_unicode))
# Plain ASCII substring inside the Unicode text must match.
check("quote unicode: ASCII substring of Unicode text",
      _quote_in_text("HR Business Partner", text_with_unicode))
# Ellipsis vs three ASCII dots: NFKC + translate folds them.
check("quote unicode: ellipsis vs three dots",
      _quote_in_text("Frau M" + UMLAUT_U + "ller...", "Frau M" + UMLAUT_U + "ller" + ELLIPSIS + " HR"))
# NBSP in source text vs ASCII space in quote -> normalized as whitespace.
check("quote unicode: NBSP in text vs ASCII space in quote",
      _quote_in_text("Frau M" + UMLAUT_U + "ller HR", "Frau M" + UMLAUT_U + "ller" + NBSP + "HR"))


# ---------------------------------------------------------------------------
# _looks_like_aggregator
# ---------------------------------------------------------------------------

aggregator_positives = (
    "linkedin.com",
    "www.linkedin.com",                  # _domain_from_url strips www, but this fn handles raw too
    "indeed.com",
    "ch.indeed.com",                     # subdomain
    "jobs.ch",
    "stepstone.de",
    "jobup.ch",
    "xing.com",
    "jobagent.ch",
    "jobs4sales.ch",
    "workpool-jobs.ch",
    "job-room.ch",
    "jobijoba.ch",
    "topjobs.ch",
    "stellen-anzeiger.ch",
    "ictjobs.ch",
    "adzuna.ch",
    "efinancialcareers.ch",
    "ch.expertini.com",
    "de-ch.whatjobs.com",
)
for host in aggregator_positives:
    check(f"aggregator: {host} is aggregator", _looks_like_aggregator(host))

check("aggregator: whitespace/trailing-dot normalized",
      _looks_like_aggregator("  jobs.ch.  "))

aggregator_negatives = (
    "roche.com",
    "careers.roche.com",
    "novartis.com",
    "vitaminwell.com",
    "company-x.ch",
    "jobs.sanitas.com",
    "jobdetails.nestle.com",
    "lonza.wd3.myworkdayjobs.com",
    "danaher.wd1.myworkdayjobs.com",
    "job-boards.eu.greenhouse.io",
    "",
    None,
)
for host in aggregator_negatives:
    check(f"aggregator: {host!r} is NOT aggregator", not _looks_like_aggregator(host))


# ---------------------------------------------------------------------------
# _domain_from_url
# ---------------------------------------------------------------------------

check("domain: https URL", _domain_from_url("https://careers.roche.com/job/123") == "careers.roche.com")
check("domain: http URL", _domain_from_url("http://example.ch") == "example.ch")
check("domain: www stripped", _domain_from_url("https://www.example.ch") == "example.ch")
check("domain: bare host", _domain_from_url("example.ch") == "example.ch")
check("domain: empty -> None", _domain_from_url("") is None)


# ---------------------------------------------------------------------------
# _email_domain_acceptable
# ---------------------------------------------------------------------------

expected_roche = {"roche.com"}

check("domain-check: company match",
      _email_domain_acceptable("jane.doe@roche.com", expected_roche))

check("domain-check: subdomain match (careers.roche.com vs roche.com)",
      _email_domain_acceptable("hr@careers.roche.com", expected_roche))

# Parent rule was DROPPED 2026-05-16 because it gave ATS hosts free hallucination passes.
# Real JDs cite the apex domain in body text, which lands via the regex pass.
check("domain-check: parent rule REJECTED (was: roche.com vs hr.roche.com expected)",
      not _email_domain_acceptable("jane@roche.com", {"hr.roche.com"}))

# Regression: ATS-subdomain hallucination vector. Listing host is a Workday tenant.
# A hallucinated email at the ATS root must NOT pass.
check("domain-check: ATS root NOT accepted via parent of tenant subdomain (workday)",
      not _email_domain_acceptable("recruiter@myworkdayjobs.com", {"acme.wd3.myworkdayjobs.com"}))
check("domain-check: ATS root NOT accepted (greenhouse)",
      not _email_domain_acceptable("hr@greenhouse.io", {"acme.greenhouse.io"}))
# eTLD false-positive guard.
check("domain-check: bare TLD-ish not accepted via subdomain rule",
      not _email_domain_acceptable("hr@co.uk", {"example.co.uk"}))

check("domain-check: free-mail gmail rejected even when expected matches",
      not _email_domain_acceptable("jane@gmail.com", {"gmail.com"}))

check("domain-check: free-mail gmx.ch rejected",
      not _email_domain_acceptable("hr@gmx.ch", {"roche.com"}))

check("domain-check: free-mail bluewin.ch rejected",
      not _email_domain_acceptable("contact@bluewin.ch", {"roche.com"}))

check("domain-check: unrelated company rejected",
      not _email_domain_acceptable("jane@novartis.com", {"roche.com"}))

check("domain-check: empty expected set rejects everything",
      not _email_domain_acceptable("jane@roche.com", set()))

check("domain-check: empty email rejected",
      not _email_domain_acceptable("", expected_roche))

check("domain-check: malformed email rejected",
      not _email_domain_acceptable("not-an-email", expected_roche))

check("domain-check: None email rejected",
      not _email_domain_acceptable(None, expected_roche))

check("domain-check: whitespace-padded email accepted (stripped)",
      _email_domain_acceptable("  jane@roche.com  ", expected_roche))


# ---------------------------------------------------------------------------
# _build_expected_domains
# ---------------------------------------------------------------------------

# Direct (non-aggregator) listing: only the listing host seeds expected_domains.
# JD-mentioned email domains are NOT used on direct listings (avoids "send NOT
# to old@oldco.com" anti-pattern leaking domains into the trust set).
job_direct = {
    "url": "https://careers.roche.com/job/12345",
    "description": "Bei Fragen wenden Sie sich an hr@roche.com.",
    "company": "Roche",
}
domains_direct = _build_expected_domains(job_direct)
check("build_expected: direct listing host included",
      "careers.roche.com" in domains_direct,
      f"got: {domains_direct}")
check("build_expected: JD emails NOT seeded on direct listings",
      "roche.com" not in domains_direct,
      f"got: {domains_direct}")

# Job on an aggregator — listing host EXCLUDED, JD-mentioned domain INCLUDED
# (only path that rescues an aggregator row).
job_aggregator = {
    "url": "https://ch.indeed.com/job/abc",
    "description": "Apply at careers.example-ch.com or email hr@example-ch.com.",
    "company": "Example CH",
}
domains_agg = _build_expected_domains(job_aggregator)
check("build_expected: aggregator host EXCLUDED",
      "ch.indeed.com" not in domains_agg and "indeed.com" not in domains_agg,
      f"got: {domains_agg}")
check("build_expected: JD-mentioned domain rescues aggregator row",
      "example-ch.com" in domains_agg,
      f"got: {domains_agg}")

for label, url, email, expected_domain in (
    (
        "jobagent.ch",
        "https://www.jobagent.ch/job/sales-business-operations-specialist/3250466671",
        "jobs@ontrex.ch",
        "ontrex.ch",
    ),
    (
        "workpool-jobs.ch",
        "https://www.workpool-jobs.ch/business-data-analyst-zuerich/123",
        "jobs@oneagency.ch",
        "oneagency.ch",
    ),
    (
        "stellen-anzeiger.ch",
        "https://www.stellen-anzeiger.ch/job/data-analyst-in-data-migration/456",
        "zeynep.ucar@coopers.ch",
        "coopers.ch",
    ),
    (
        "jobs4sales.ch",
        "https://www.jobs4sales.ch/job/head-real-estate-sales-a/789",
        "dragan.popovic@s-p.ch",
        "s-p.ch",
    ),
    (
        "job-room.ch",
        "https://www.job-room.ch/job/business-analyst/123",
        "recruiting@example-room.ch",
        "example-room.ch",
    ),
    (
        "jobijoba.ch",
        "https://www.jobijoba.ch/detail/1/abc",
        "hr@example-ijoba.ch",
        "example-ijoba.ch",
    ),
    (
        "topjobs.ch",
        "https://www.topjobs.ch/job/data-analyst/123",
        "hr@example-top.ch",
        "example-top.ch",
    ),
    (
        "efinancialcareers.ch",
        "https://www.efinancialcareers.ch/jobs/abc",
        "recruiting@ffiadvisory.ch",
        "ffiadvisory.ch",
    ),
    (
        "ch.expertini.com",
        "https://ch.expertini.com/jobs/job/abc",
        "hr@example-expertini-client.ch",
        "example-expertini-client.ch",
    ),
    (
        "de-ch.whatjobs.com",
        "https://de-ch.whatjobs.com/job/abc",
        "hr@example-what-client.ch",
        "example-what-client.ch",
    ),
):
    domains = _build_expected_domains({
        "url": url,
        "description": f"Bei Fragen wenden Sie sich bitte an {email}.",
        "company": "Example",
    })
    listing_host = _domain_from_url(url)
    check(f"build_expected: {label} host excluded",
          listing_host not in domains,
          f"got: {domains}")
    check(f"build_expected: {label} JD email domain included",
          expected_domain in domains,
          f"got: {domains}")

for label, url, description, expected_domains in (
    (
        "jobs.ch support email only",
        "https://www.jobs.ch/job/123",
        "Questions about the platform? Email support@jobs.ch.",
        set(),
    ),
    (
        "indeed privacy email only",
        "https://ch.indeed.com/job/abc",
        "For platform privacy requests contact privacy@indeed.com.",
        set(),
    ),
    (
        "jobagent support plus real company email",
        "https://www.jobagent.ch/job/abc",
        "Jobagent support: support@jobagent.ch. For applications: jobs@ontrex.ch.",
        {"ontrex.ch"},
    ),
):
    domains = _build_expected_domains({
        "url": url,
        "description": description,
        "company": "Example",
    })
    check(f"build_expected: {label} skips aggregator-owned domains",
          domains == expected_domains,
          f"got: {domains}")

for label, url, email, expected_host, rejected_domain in (
    (
        "company jobs subdomain",
        "https://jobs.sanitas.com/job/123",
        "hr@sanitas.com",
        "jobs.sanitas.com",
        "sanitas.com",
    ),
    (
        "company jobdetails host",
        "https://jobdetails.nestle.com/job/456",
        "hr@nestle.com",
        "jobdetails.nestle.com",
        "nestle.com",
    ),
    (
        "workday tenant",
        "https://lonza.wd3.myworkdayjobs.com/job/789",
        "hr@lonza.com",
        "lonza.wd3.myworkdayjobs.com",
        "lonza.com",
    ),
    (
        "greenhouse tenant",
        "https://job-boards.eu.greenhouse.io/acme/jobs/123",
        "hr@acme.com",
        "job-boards.eu.greenhouse.io",
        "acme.com",
    ),
):
    domains = _build_expected_domains({
        "url": url,
        "description": f"Bei Fragen wenden Sie sich bitte an {email}.",
        "company": "Example",
    })
    check(f"build_expected: {label} direct host included",
          expected_host in domains,
          f"got: {domains}")
    check(f"build_expected: {label} JD email domain not seeded",
          rejected_domain not in domains,
          f"got: {domains}")

# JD only mentions a free-mail address — free-mail must NOT seed expected set.
job_freemail_only = {
    "url": "https://jobs.ch/listing/123",
    "description": "Send your CV to recruiter.smith@gmail.com.",
    "company": "Tiny GmbH",
}
domains_free = _build_expected_domains(job_freemail_only)
check("build_expected: free-mail does NOT seed expected set",
      "gmail.com" not in domains_free,
      f"got: {domains_free}")
check("build_expected: aggregator + free-mail-only JD => empty expected set",
      domains_free == set(),
      f"got: {domains_free}")


# ---------------------------------------------------------------------------
# _score_apply_link (canonical-apply scoring helper)
# ---------------------------------------------------------------------------

# ATS host alone clears the threshold.
check("apply-score: workday tenant URL clears threshold",
      _score_apply_link("https://acme.wd3.myworkdayjobs.com/job/123", "Apply", "ch.indeed.com")
      >= _APPLY_MIN_SCORE)
check("apply-score: greenhouse URL clears threshold",
      _score_apply_link("https://boards.greenhouse.io/acme/jobs/4567", "Apply", "linkedin.com")
      >= _APPLY_MIN_SCORE)
check("apply-score: lever URL clears threshold",
      _score_apply_link("https://jobs.lever.co/acme/abc-def", "Apply Now", "linkedin.com")
      >= _APPLY_MIN_SCORE)
# Strong text on non-ATS host clears threshold.
check("apply-score: 'apply on company site' text clears threshold",
      _score_apply_link("https://careers.acme.ch/job/123", "Apply on company site", "ch.indeed.com")
      >= _APPLY_MIN_SCORE)
check("apply-score: 'auf der unternehmenswebsite' clears threshold",
      _score_apply_link("https://acme.ch/jobs/abc", "auf der Unternehmenswebsite bewerben", "ch.indeed.com")
      >= _APPLY_MIN_SCORE)
# Weak text alone (bare "Apply") on a generic host does NOT clear threshold —
# this is the "similar jobs carousel" false-positive guard.
check("apply-score: bare 'Apply' on unrelated host BELOW threshold",
      _score_apply_link("https://somerandomsite.com/page", "Apply", "ch.indeed.com")
      < _APPLY_MIN_SCORE)
# Same-host links are filtered out.
check("apply-score: same-host link scores 0",
      _score_apply_link("https://ch.indeed.com/job/other", "Apply on company site", "ch.indeed.com") == 0)
# Aggregator sister hosts filtered.
check("apply-score: indeed → linkedin scores 0 (aggregator sibling)",
      _score_apply_link("https://www.linkedin.com/jobs/view/123", "Apply Now", "ch.indeed.com") == 0)
# Social / tracker hosts filtered.
check("apply-score: facebook share link scores 0",
      _score_apply_link("https://www.facebook.com/sharer/sharer.php?u=...", "Apply", "ch.indeed.com") == 0)
# Malformed input → 0.
check("apply-score: empty href → 0",
      _score_apply_link("", "Apply", "ch.indeed.com") == 0)
check("apply-score: non-http scheme → 0",
      _score_apply_link("mailto:hr@example.com", "Apply", "ch.indeed.com") == 0)


# ---------------------------------------------------------------------------
# _transliterate_to_ascii (Source 3 — name → ASCII for email construction)
# ---------------------------------------------------------------------------

check("translit: Müller → mueller",
      _transliterate_to_ascii("Müller") == "mueller",
      f"got: {_transliterate_to_ascii('Müller')!r}")
check("translit: Schlüsselbänder → schluesselbaender",
      _transliterate_to_ascii("Schlüsselbänder") == "schluesselbaender",
      f"got: {_transliterate_to_ascii('Schlüsselbänder')!r}")
check("translit: Görg → goerg",
      _transliterate_to_ascii("Görg") == "goerg")
check("translit: Renée → renee (NFKD accent strip)",
      _transliterate_to_ascii("Renée") == "renee",
      f"got: {_transliterate_to_ascii('Renée')!r}")
check("translit: Côté → cote",
      _transliterate_to_ascii("Côté") == "cote")
check("translit: Straße → strasse (ß digraph)",
      _transliterate_to_ascii("Straße") == "strasse")
_obrien_result = _transliterate_to_ascii("O'Brien")
check("translit: O'Brien → obrien (apostrophe stripped)",
      _obrien_result == "obrien",
      f"got: {_obrien_result!r}")
check("translit: Müller-Schmidt → mueller-schmidt (hyphen preserved)",
      _transliterate_to_ascii("Müller-Schmidt") == "mueller-schmidt",
      f"got: {_transliterate_to_ascii('Müller-Schmidt')!r}")
check("translit: empty string → empty",
      _transliterate_to_ascii("") == "")
check("translit: ALL CAPS Ü → ue",
      _transliterate_to_ascii("ÜBER") == "ueber",
      f"got: {_transliterate_to_ascii('ÜBER')!r}")


# ---------------------------------------------------------------------------
# _split_name_for_email
# ---------------------------------------------------------------------------

check("split-name: simple 'Stefan Meier' → (stefan, meier)",
      _split_name_for_email("Stefan Meier") == ("stefan", "meier"),
      f"got: {_split_name_for_email('Stefan Meier')!r}")
check("split-name: honorific 'Frau Anna Müller' → (anna, mueller)",
      _split_name_for_email("Frau Anna Müller") == ("anna", "mueller"),
      f"got: {_split_name_for_email('Frau Anna Müller')!r}")
check("split-name: 'Herr Stefan Meier' strips Herr",
      _split_name_for_email("Herr Stefan Meier") == ("stefan", "meier"))
check("split-name: 'Dr. Renée Côté' strips title",
      _split_name_for_email("Dr. Renée Côté") == ("renee", "cote"),
      f"got: {_split_name_for_email('Dr. Renée Côté')!r}")
check("split-name: middle name → still (first, last)",
      _split_name_for_email("Anna Maria Müller") == ("anna", "mueller"),
      f"got: {_split_name_for_email('Anna Maria Müller')!r}")
check("split-name: hyphenated surname preserved",
      _split_name_for_email("Anna Müller-Schmidt") == ("anna", "mueller-schmidt"),
      f"got: {_split_name_for_email('Anna Müller-Schmidt')!r}")
check("split-name: single token 'Müller' → None (ambiguous)",
      _split_name_for_email("Müller") is None,
      f"got: {_split_name_for_email('Müller')!r}")
check("split-name: empty → None",
      _split_name_for_email("") is None)
check("split-name: whitespace-only → None",
      _split_name_for_email("   ") is None)
check("split-name: 'Sig. Marco Rossi' strips Italian honorific",
      _split_name_for_email("Sig. Marco Rossi") == ("marco", "rossi"),
      f"got: {_split_name_for_email('Sig. Marco Rossi')!r}")


# ---------------------------------------------------------------------------
# _infer_email_pattern
# ---------------------------------------------------------------------------

check("infer: anna.mueller@x.com → {first}.{last}",
      _infer_email_pattern("anna.mueller@x.com", "x.com") == "{first}.{last}",
      f"got: {_infer_email_pattern('anna.mueller@x.com', 'x.com')!r}")
check("infer: ANNA.MUELLER@X.COM normalized → {first}.{last}",
      _infer_email_pattern("ANNA.MUELLER@X.COM", "x.com") == "{first}.{last}")
check("infer: anna_mueller@x.com → {first}_{last}",
      _infer_email_pattern("anna_mueller@x.com", "x.com") == "{first}_{last}")
check("infer: amueller@x.com (5+ chars single token) → {f}{last}",
      _infer_email_pattern("amueller@x.com", "x.com") == "{f}{last}",
      f"got: {_infer_email_pattern('amueller@x.com', 'x.com')!r}")
check("infer: ab@x.com (too short single token) → None",
      _infer_email_pattern("ab@x.com", "x.com") is None,
      f"got: {_infer_email_pattern('ab@x.com', 'x.com')!r}")
check("infer: 3-token local 'first.middle.last' → None (ambiguous)",
      _infer_email_pattern("anna.maria.mueller@x.com", "x.com") is None,
      f"got: {_infer_email_pattern('anna.maria.mueller@x.com', 'x.com')!r}")
check("infer: malformed (no @) → None",
      _infer_email_pattern("notanemail", "x.com") is None)
check("infer: empty → None",
      _infer_email_pattern("", "x.com") is None)


# ---------------------------------------------------------------------------
# _construct_email
# ---------------------------------------------------------------------------

check("construct: first.last pattern",
      _construct_email("anna", "mueller", "{first}.{last}", "x.com") == "anna.mueller@x.com",
      f"got: {_construct_email('anna', 'mueller', '{first}.{last}', 'x.com')!r}")
check("construct: f+last pattern",
      _construct_email("anna", "mueller", "{f}{last}", "x.com") == "amueller@x.com")
check("construct: first+l pattern",
      _construct_email("anna", "mueller", "{first}{l}", "x.com") == "annam@x.com")
check("construct: firstlast (no separator)",
      _construct_email("anna", "mueller", "{first}{last}", "x.com") == "annamueller@x.com")
check("construct: last.first reversed",
      _construct_email("anna", "mueller", "{last}.{first}", "x.com") == "mueller.anna@x.com")
check("construct: first-only pattern",
      _construct_email("anna", "mueller", "{first}", "x.com") == "anna@x.com")
check("construct: uppercase domain lowercased",
      _construct_email("anna", "mueller", "{first}.{last}", "X.COM") == "anna.mueller@x.com")
check("construct: empty first → None",
      _construct_email("", "mueller", "{first}.{last}", "x.com") is None)
check("construct: empty domain → None",
      _construct_email("anna", "mueller", "{first}.{last}", "") is None)
check("construct: unknown pattern → None",
      _construct_email("anna", "mueller", "{nope}", "x.com") is None)


# ---------------------------------------------------------------------------
# _resolve_company_domain_free (SerpAPI-free domain resolution)
# ---------------------------------------------------------------------------

# Step 1 — job URL host wins when it's not an aggregator.
check("resolve: job URL on company domain wins",
      _resolve_company_domain_free(
          "Acmecorp AG",
          "https://acmecorp.ch/jobs/sales-rep",
          "",
      ) == "acmecorp.ch")

# Step 1 short-circuits even if email domain disagrees.
check("resolve: job URL preferred over email domain",
      _resolve_company_domain_free(
          "Acmecorp AG",
          "https://acmecorp.ch/jobs/sales-rep",
          "Send CV to careers@partner.com",
      ) == "acmecorp.ch")

# Step 2 — falls through to email-extracted domain when job URL is aggregator.
check("resolve: email-extracted domain when job on aggregator",
      _resolve_company_domain_free(
          "Acmecorp AG",
          "https://linkedin.com/jobs/view/1234",
          "Send your CV to anna.mueller@acmecorp.ch — questions to careers@acmecorp.ch.",
      ) == "acmecorp.ch")

# Aggregator job URL + no useful email + nonsensical company → no resolution.
check("resolve: no match returns None",
      _resolve_company_domain_free(
          "Zzqqxxx",
          "https://linkedin.com/jobs/view/1234",
          "Apply via portal.",
      ) is None)

# Free-mail in description shouldn't be returned even if extracted.
check("resolve: free-mail domain not returned",
      _resolve_company_domain_free(
          "Random GmbH",
          "https://linkedin.com/jobs/view/1234",
          "Reach me at randomguy@gmail.com.",
      ) != "gmail.com")

# Empty company + aggregator URL → None.
check("resolve: empty company returns None",
      _resolve_company_domain_free("", "https://indeed.com/viewjob?jk=x", "") is None)


# ---------------------------------------------------------------------------
# _rescue_pending_emails — pending-email rescue with confidence routing
# ---------------------------------------------------------------------------

def _fresh_out() -> dict:
    return {
        "contact_name": None,
        "contact_email": None,
        "contact_title": None,
        "contact_honorific": None,
        "contact_source": "NOT_FOUND",
        "contact_confidence": "",
    }


# Acme Robotics — careers@ recruiter inbox, domain word-matches → medium.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="Acme Robotics",
    pending_emails=[("careers@acmerobotics.co", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: recruiter inbox + company-name match → medium",
      _aug is not None
      and _out["contact_email"] == "careers@acmerobotics.co"
      and _out["contact_confidence"] == "medium"
      and _out["contact_source"] == "Listing_Page",
      f"out={_out}, aug={_aug}")

# Acmecorp AG — person-specific email, domain word-matches → high.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="Acmecorp AG",
    pending_emails=[("anna.mueller@acmecorp.ch", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: person-specific + company-name match → high",
      _aug is not None
      and _out["contact_email"] == "anna.mueller@acmecorp.ch"
      and _out["contact_confidence"] == "high",
      f"out={_out}, aug={_aug}")

# No company-name match → no rescue.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="Acme Robotics",
    pending_emails=[("careers@somerandom.com", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: no word-match → no rescue",
      _aug is None and _out["contact_email"] is None,
      f"out={_out}, aug={_aug}")

# Free-mail rejected even when local-part is careers@.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="Random GmbH",
    pending_emails=[("careers@gmail.com", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: free-mail domain rejected",
      _aug is None,
      f"aug={_aug}")

# Aggregator-host email rejected.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="Sika Corporation",
    pending_emails=[("careers@indeed.com", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: aggregator-host email rejected",
      _aug is None,
      f"aug={_aug}")

# Short company name (no 4+ char tokens) → no rescue from this helper.
_out = _fresh_out()
_aug = _rescue_pending_emails(
    out=_out,
    company="LHH",
    pending_emails=[("careers@lhh.com", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: short company name with no 4+ char tokens → no rescue",
      _aug is None,
      f"aug={_aug}")

# Pre-set contact_email → bail without writing.
_out = _fresh_out()
_out["contact_email"] = "preset@x.com"
_aug = _rescue_pending_emails(
    out=_out,
    company="Acmecorp AG",
    pending_emails=[("anna.mueller@acmecorp.ch", "Listing_Page")],
    cache=None,
    company_key="",
    expected_domains=set(),
)
check("rescue: bails when contact_email already set",
      _aug is None and _out["contact_email"] == "preset@x.com",
      f"out={_out}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

if _failures:
    print(f"FAILED {len(_failures)} / {_passes + len(_failures)} checks:")
    for line in _failures:
        print(line)
    sys.exit(1)
else:
    print(f"OK {_passes} / {_passes} checks passed.")
    sys.exit(0)
