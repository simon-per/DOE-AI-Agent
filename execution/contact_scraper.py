"""Impressum / Kontakt page scraper for contact-discovery Source 2.

Swiss + DE + AT companies are legally required to publish a contact (Impressum / Kontakt).
This module fetches a small candidate set of well-known paths, extracts visible text via
Playwright (already bundled in `image_with_playwright`), and asks the LLM to pull out
structured `[{name, email, title}, …]`.

Failures are silent — every exception path returns `[]` so the caller (`discover_contacts.py`)
can move on to the next source without aborting the pipeline.

Usage:
    from execution.contact_scraper import scrape_company_contacts
    contacts = scrape_company_contacts("https://example.ch")
    # → [{"name": "Anna Müller", "email": "anna@example.ch", "title": "HR", "source_url": "..."}]
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin, urlparse

log = logging.getLogger(__name__)

# Paths tried in order — Impressum first (legally required = highest yield), then Kontakt,
# then English-language equivalents, then team / about pages as a last resort.
_CANDIDATE_PATHS = (
    "/impressum",
    "/de/impressum",
    "/legal/impressum",
    "/kontakt",
    "/de/kontakt",
    "/contact",
    "/en/contact",
    "/contact-us",
    "/team",
    "/about/team",
    "/people",
    "/karriere",
    "/jobs/team",
    "/about",
)

# Per-page Playwright timeouts. Generous enough for slow CMS, short enough that 3 pages
# x 10s = 30s worst case. Beyond that we bail out and let the caller move on.
_PAGE_GOTO_TIMEOUT_MS = 10_000
_BODY_TEXT_MAX_CHARS = 8_000  # cap before sending to LLM


def _normalize_url(domain_or_url: str) -> str | None:
    """Accept 'example.ch', 'http://example.ch', 'https://www.example.ch/jobs' → return base url."""
    if not domain_or_url:
        return None
    raw = domain_or_url.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    if not parsed.netloc:
        return None
    # Always use https as the canonical scheme — most modern sites redirect anyway
    return f"https://{parsed.netloc}"


# Unused after 2026-05-16 listing-anchored refactor — kept for now in case the
# company-level Impressum fallback is reintroduced. `discover_contacts.py` now
# uses `fetch_listing_page_text()` below to re-fetch the listing's own URL.
def scrape_company_contacts(
    domain_or_url: str,
    openrouter_key: str | None = None,
    gemini_key: str | None = None,
    browser=None,
    max_paths: int = 4,
) -> list[dict]:
    """Fetch up to `max_paths` candidate pages and LLM-extract contact entries.

    Args:
        domain_or_url: 'example.ch' / 'https://example.ch' / 'https://www.example.ch/jobs'
        openrouter_key, gemini_key: passed through to the LLM extractor; if both None,
            we fall back to a regex-only extraction (less accurate but free).
        browser: optional reusable Playwright browser handle. If None, we launch a fresh
            chromium, scrape, and close it. Reusing across many companies is ~3x faster.
        max_paths: hard cap on the number of paths probed per company.

    Returns:
        List of contact dicts: `[{"name", "email", "title", "source_url", "source": "impressum"|...}, …]`.
        Empty list on any failure (network, malformed HTML, missing playwright, etc.).
        Never raises.
    """
    base = _normalize_url(domain_or_url)
    if not base:
        return []

    # Lazy import — playwright takes ~50ms to import, only pay if we actually use it
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("  playwright not installed; contact_scraper inactive")
        return []

    results: list[dict] = []
    pw_ctx = None
    own_browser = False

    try:
        if browser is None:
            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=True)
            own_browser = True

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        for path in _CANDIDATE_PATHS[:max_paths]:
            url = urljoin(base, path)
            try:
                page.goto(url, timeout=_PAGE_GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            except PWTimeout:
                log.debug(f"  scrape: timeout for {url}")
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug(f"  scrape: navigation failed for {url}: {type(exc).__name__}")
                continue

            # Quick filter — many CMS return a soft 200 with a "page not found" body
            try:
                status = page.url  # if the server redirected to /404 or similar
                if any(token in status.lower() for token in ("/404", "/not-found", "/error")):
                    continue
                body_text = page.evaluate("() => document.body && document.body.innerText || ''")
            except Exception as exc:  # noqa: BLE001
                log.debug(f"  scrape: body read failed for {url}: {type(exc).__name__}")
                continue

            if not body_text or len(body_text) < 80:
                continue

            extracted = _extract_contacts_from_text(
                body_text[:_BODY_TEXT_MAX_CHARS],
                source_url=url,
                source_label=_label_for_path(path),
                openrouter_key=openrouter_key,
                gemini_key=gemini_key,
            )
            if extracted:
                results.extend(extracted)
                # Stop after we've collected at least one good hit — minimize cost
                if any(e.get("email") for e in extracted):
                    break

        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass

    except Exception as exc:  # noqa: BLE001 — Playwright fails in many ways
        log.warning(f"  scrape_company_contacts({domain_or_url}) failed: {type(exc).__name__}: {exc}")
    finally:
        if own_browser and browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if pw_ctx is not None:
            try:
                pw_ctx.stop()
            except Exception:  # noqa: BLE001
                pass

    return results


_LISTING_PAGE_MAX_CHARS = 24_000  # raw fetch cap. The LLM extractor downsamples
# to its own _LLM_TEXT_MAX_CHARS via head+tail composition, so we capture both
# the page header (sometimes has sidebar contact widget) and the page footer
# (where "Ihre Ansprechpartnerin …" almost always sits in DE/CH postings).

# LinkedIn renders a "Meet the hiring team" / "Posted by" card with the
# recruiter's name and title. Class names rotate quarterly, so we try multiple
# selector strategies and fall back to text-anchored locators.
_LINKEDIN_HIRING_TEAM_SELECTORS = (
    '[data-test-modules="hiring-team-card"]',
    '[class*="hiring-team"]',
    'section:has-text("Meet the hiring team")',
    'section:has-text("Hiring team")',
    'div:has-text("Posted by") >> nth=0',
)


def _extract_linkedin_hiring_team_text(page) -> str:
    """Return text content of LinkedIn's hiring-team card, or ''.

    Called inline by fetch_listing_page_text when the URL host is linkedin.com
    so we don't double-load the page. Always returns ''; never raises. The
    returned text is prepended to the page body so the LLM extractor sees the
    recruiter's name with a clear header.
    """
    for sel in _LINKEDIN_HIRING_TEAM_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            text = loc.inner_text(timeout=2_000)
            if text and text.strip() and len(text.strip()) <= 800:
                # Sanity cap — a real hiring-team card is short. If the selector
                # accidentally grabbed an entire page section, skip it.
                return text.strip()
        except Exception:  # noqa: BLE001 — Playwright fails in many ways
            continue
    return ""


def fetch_listing_page_text(
    url: str,
    *,
    browser=None,
    timeout_ms: int = 20_000,
) -> str:
    """Re-fetch a single job posting URL and return its rendered visible text.

    Used as Source 2 of the contact-discovery waterfall: when the LLM finds no
    contact in the JD text stored at scrape time, we re-fetch the listing's own
    page and re-extract. Sidebars, contact widgets, and `<a href="mailto:...">`
    buttons survive in the rendered DOM even though they were stripped from the
    cached `description` field.

    Args:
        url: The listing's own URL (job['url']).
        browser: Optional reusable Playwright browser handle. If None, we launch
            a fresh chromium for this single call.
        timeout_ms: Per-call hard cap for page navigation.

    Returns:
        Visible body text up to `_LISTING_PAGE_MAX_CHARS`. Empty string on any
        failure (network, captcha wall, missing playwright, malformed URL).
        Never raises.
    """
    if not url:
        return ""

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("  playwright not installed; fetch_listing_page_text inactive")
        return ""

    pw_ctx = None
    own_browser = False
    text = ""

    try:
        if browser is None:
            pw_ctx = sync_playwright().start()
            browser = pw_ctx.chromium.launch(headless=True)
            own_browser = True

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        # Guarantee context.close() runs even if new_page() or page.* raises.
        # A leaked context holds a ~30-50 MB Chromium process — across a 250-row
        # backlog one leak per ~10 failures can OOM the Modal container.
        try:
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Small settle wait — many ATS systems (Workday, SmartRecruiters,
                # SAP SuccessFactors) hydrate contact widgets via JS just after
                # domcontentloaded fires. Bounded by timeout_ms / 10 so we don't
                # double the per-row budget.
                try:
                    page.wait_for_load_state("networkidle", timeout=min(5_000, timeout_ms // 4))
                except PWTimeout:
                    pass
            except PWTimeout:
                log.info(f"  listing-page fetch timeout: {url}")
            except Exception as exc:  # noqa: BLE001
                log.info(f"  listing-page fetch failed ({type(exc).__name__}): {url}")
            else:
                try:
                    body = page.evaluate(
                        "() => document.body && document.body.innerText || ''"
                    )
                    if isinstance(body, str) and body:
                        text = body[:_LISTING_PAGE_MAX_CHARS]
                except Exception as exc:  # noqa: BLE001
                    log.debug(f"  listing-page body read failed for {url}: {type(exc).__name__}")

                # LinkedIn-only: prepend the hiring-team card with a clear header
                # so the LLM extractor sees "Posted by [Name]" right at the top.
                if "linkedin.com" in url.lower():
                    hiring_team = _extract_linkedin_hiring_team_text(page)
                    if hiring_team:
                        text = (
                            "=== HIRING TEAM (LinkedIn) ===\n"
                            f"{hiring_team}\n"
                            "=== END HIRING TEAM ===\n\n"
                            + text
                        )[:_LISTING_PAGE_MAX_CHARS]
                        log.info(f"  LinkedIn hiring-team card extracted ({len(hiring_team)} chars)")
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    except Exception as exc:  # noqa: BLE001 — playwright fails in many ways
        log.warning(f"  fetch_listing_page_text({url}) crashed: {type(exc).__name__}: {exc}")
    finally:
        if own_browser and browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        if pw_ctx is not None:
            try:
                pw_ctx.stop()
            except Exception:  # noqa: BLE001
                pass

    return text


# ---------------------------------------------------------------------------
# Aggregator → canonical apply-URL hop
# ---------------------------------------------------------------------------
#
# Aggregator listings (LinkedIn, Indeed, jobs.ch, Stepstone, etc.) re-host the
# JD text but their pages don't carry the recruiter contact — that lives on the
# "Apply on company site" link the aggregator exposes. This helper opens the
# aggregator page, picks the most-likely external apply URL, and returns it.
# Caller (discover_contacts.Source 2) then re-fetches the canonical URL and
# re-runs extraction. Still listing-anchored — the link came from the listing.

# High-signal apply text. "apply on company site" / "auf der unternehmenswebsite"
# are clearer than bare "apply" (which appears in carousels and footers).
_APPLY_TEXT_PATTERNS_STRONG = (
    "apply on company",
    "apply on the company",
    "auf der unternehmenswebsite",
    "auf unternehmenswebsite",
    "auf der website des unternehmens",
    "company website",
    "company site",
    "external application",
)

# Weak signals — only count when combined with an ATS-host match.
_APPLY_TEXT_PATTERNS_WEAK = (
    "apply now",
    "apply",
    "jetzt bewerben",
    "bewerben",
)

_ATS_HOST_PATTERNS = (
    "workday", "myworkday",
    "greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
    "successfactors.com", "successfactors.eu",
    "jobvite.com",
    "ashbyhq.com",
    "recruitee.com",
    "personio.de", "personio.com",
    "softgarden.io", "softgarden.de",
    "icims.com",
    "taleo.net",
    "workable.com",
    "bamboohr.com",
    "rippling.com",
    "join.com",
)

# Score threshold for a canonical-apply candidate. ATS-host match alone (+5)
# always qualifies; "company-site"-style text alone (+3) qualifies; bare
# "apply"/"bewerben" text only qualifies when paired with an ATS host.
_APPLY_MIN_SCORE = 3


def _score_apply_link(href: str, text: str, original_host: str) -> int:
    """Pure scoring helper for `find_canonical_apply_url`. Unit-testable.

    Returns 0 when the link is on the same host, on a known aggregator /
    social / tracker host, or has neither a useful text nor href signal.
    Otherwise returns a small integer the caller compares against
    `_APPLY_MIN_SCORE`.
    """
    if not href or not isinstance(href, str):
        return 0
    if not href.startswith(("http://", "https://")):
        return 0
    from urllib.parse import urlparse
    try:
        host = (urlparse(href).netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:  # noqa: BLE001
        return 0
    if not host or host == original_host:
        return 0
    # Skip social / tracker / aggregator-sibling hosts.
    if any(noise in host for noise in (
        "facebook.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "pinterest.com",
        "doubleclick", "googletag", "addthis", "google-analytics",
    )):
        return 0
    if any(host == agg or host.endswith("." + agg) for agg in (
        "linkedin.com", "indeed.com", "indeed.de", "indeed.ch",
        "glassdoor.com", "stepstone.de", "stepstone.ch",
        "jobs.ch", "jobup.ch", "jobcloud.ch", "jobscout24.ch",
        "monster.com", "monster.de", "monster.ch", "xing.com",
    )):
        return 0

    text_low = (text or "").strip().lower()
    has_ats = any(pat in host for pat in _ATS_HOST_PATTERNS)
    has_strong_text = any(pat in text_low for pat in _APPLY_TEXT_PATTERNS_STRONG)
    has_weak_text = any(pat in text_low for pat in _APPLY_TEXT_PATTERNS_WEAK)

    score = 0
    if has_ats:
        score += 5
    if has_strong_text:
        score += 3
    # Weak text only earns a point when paired with an ATS host —
    # bare "Apply" with no ATS context is too easy to false-positive on
    # "similar jobs" carousels.
    if has_weak_text and has_ats:
        score += 1
    return score


def find_canonical_apply_url(
    url: str,
    browser,
    timeout_ms: int = 15_000,
) -> str | None:
    """For an aggregator listing, return the external 'Apply on company site' link.

    Strategy:
      - Open the page, evaluate JS to read every `<a>`'s href + visible text.
      - Filter to cross-domain hrefs (not same host as the original URL, not
        an obvious noise host like a tracker / social network / aggregator
        sister site).
      - Score: text-match against apply patterns (+3), href matches a known
        ATS host (+5), both (+8). Pick the highest-scoring link.
      - Return None when no candidate scored at least 1.

    Bounded and silent — any failure returns None, never raises. Reuses the
    browser handle to avoid a second Playwright cold-start.
    """
    if not url or browser is None:
        return None

    try:
        from playwright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        return None

    from urllib.parse import urlparse

    try:
        original_host = (urlparse(url).netloc or "").lower()
        if original_host.startswith("www."):
            original_host = original_host[4:]
    except Exception:  # noqa: BLE001
        return None

    context = None
    try:
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        try:
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=min(5_000, timeout_ms // 3))
                except PWTimeout:
                    pass
            except PWTimeout:
                log.info(f"  canonical-apply lookup timeout: {url}")
                return None
            except Exception as exc:  # noqa: BLE001
                log.info(f"  canonical-apply lookup failed ({type(exc).__name__}): {url}")
                return None

            try:
                links = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        href: a.href || '',
                        text: (a.innerText || a.textContent || '').trim().slice(0, 120),
                    }))"""
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(f"  canonical-apply link eval failed for {url}: {type(exc).__name__}")
                return None
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning(f"  find_canonical_apply_url({url}) crashed: {type(exc).__name__}: {exc}")
        return None

    if not isinstance(links, list):
        return None

    scored: list[tuple[int, str]] = []
    for item in links:
        if not isinstance(item, dict):
            continue
        href = (item.get("href") or "").strip()
        text = item.get("text") or ""
        score = _score_apply_link(href, text, original_host)
        if score >= _APPLY_MIN_SCORE:
            scored.append((score, href))

    if not scored:
        return None

    scored.sort(key=lambda t: t[0], reverse=True)
    top_score, top_url = scored[0]
    # Reject ambiguous results: two distinct URLs tied at the top score means
    # we can't reliably pick the right one. Safer to skip than to follow the
    # wrong canonical and pollute the cache. Same-URL ties are fine (page
    # repeated the same apply CTA in header + body).
    if len(scored) > 1 and scored[1][0] == top_score and scored[1][1] != top_url:
        log.info(
            f"  canonical-apply ambiguous (top-2 tied at score={top_score}): "
            f"{top_url} vs {scored[1][1]}"
        )
        return None

    log.info(f"  canonical-apply candidate (score={top_score}): {top_url}")
    return top_url


def _label_for_path(path: str) -> str:
    """Categorize a path into a coarse confidence-bearing label."""
    p = path.lower()
    if "impressum" in p:
        return "impressum"
    if "kontakt" in p or "contact" in p:
        return "kontakt"
    if "team" in p or "people" in p:
        return "team"
    if "karriere" in p or "jobs" in p:
        return "karriere"
    return "about"


def _extract_contacts_from_text(
    text: str,
    source_url: str,
    source_label: str,
    openrouter_key: str | None,
    gemini_key: str | None,
) -> list[dict]:
    """LLM-parse a page's visible text for contact entries.

    Returns at most ~6 contacts (we only need one or two — recruiter / HR / hiring manager).
    Falls back to a pure-regex extraction if no LLM keys are available or the LLM call fails.
    """
    # Regex-only fallback path (free, deterministic) — captures emails, surrounding name/title
    # is left for downstream logic
    if not (openrouter_key or gemini_key):
        return _regex_only_extract(text, source_url, source_label)

    prompt = (
        "From the website text below, extract the people that a job applicant could write to "
        "(HR, recruiters, hiring managers, talent acquisition, contact persons). "
        "Return ONLY a valid JSON object with this schema:\n"
        '{"contacts": [{"name": "...", "email": "...", "title": "..."}, ...]}\n\n'
        "Rules:\n"
        "- Return at most 6 entries; ordered most-relevant-first (HR / Talent first, "
        "then leadership, then anyone else with an email).\n"
        "- Skip generic mailboxes (info@, hr@, jobs@) UNLESS that's the only one given.\n"
        "- name / email / title may each be null if not stated. Don't invent values.\n"
        "- If no people are listed, return {\"contacts\": []}.\n\n"
        f"Website text:\n{text}"
    )

    try:
        from execution.llm_client import call_llm, parse_json_response
        response, provider = call_llm(
            openrouter_key, gemini_key, prompt,
            temperature=0.1, max_tokens=600, json_mode=True,
        )
        data = parse_json_response(response) or {}
        raw = data.get("contacts") or []
        out: list[dict] = []
        for entry in raw[:6]:
            if not isinstance(entry, dict):
                continue
            name = _coerce_str(entry.get("name"))
            email = _coerce_str(entry.get("email"))
            title = _coerce_str(entry.get("title"))
            if not (name or email):
                continue
            out.append({
                "name": name,
                "email": email,
                "title": title,
                "source_url": source_url,
                "source": source_label,
            })
        if out:
            log.info(f"  scrape: {len(out)} contact(s) from {source_url} (via {provider})")
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug(f"  LLM scrape parse failed for {source_url}: {type(exc).__name__}: {exc}")
        return _regex_only_extract(text, source_url, source_label)


def _coerce_str(val) -> Optional[str]:
    if isinstance(val, str) and val.strip() and val.strip().lower() not in ("null", "none", "n/a"):
        return val.strip()
    return None


# ---------------------------------------------------------------------------
# Source 3 — pattern-anchored email discovery
# ---------------------------------------------------------------------------
# Lightweight HTTP fetch (no Playwright) of well-known contact surfaces on a
# company's own domain. Returns the raw set of same-domain emails so the
# caller can classify (person-specific vs. useful-generic vs. harmless-generic)
# and infer a pattern.

# Paths tried in order. Impressum is legally required in DE/CH/AT and is the
# most reliable source of a contact email.
_COMPANY_CONTACT_PATHS = (
    # Legally-required first (highest hit rate in DE/CH/AT)
    "/impressum",
    "/de/impressum",
    "/imprint",
    "/en/imprint",
    "/legal-notice",
    "/legal/imprint",
    # Contact pages
    "/kontakt",
    "/de/kontakt",
    "/contact",
    "/en/contact",
    "/contact-us",
    # Team / management / leadership — often the only place mid-size Swiss companies
    # expose direct emails (impressum may have a form, but team pages list executives).
    "/team",
    "/de/team",
    "/en/team",
    "/our-team",
    "/management",
    "/de/management",
    "/leadership",
    "/de/leadership",
    "/about-us/team",
    "/about/team",
    "/ueber-uns/team",
    "/people",
    "/about-us/people",
    "/wer-wir-sind",
    # Fallback
    "/about",
    "/about-us",
    "/",
)

_COMPANY_CONTACT_MAX_PAGES = 7  # bound: stop after we've fetched this many pages


def find_company_contact_emails(domain: str) -> list[str]:
    """Scan well-known contact surfaces on `domain` for same-domain emails.

    Tries impressum / contact / home (up to _COMPANY_CONTACT_MAX_PAGES paths),
    fetches with the lightweight requests-based `fetch_page_text` (no Playwright),
    extracts emails with `_EMAIL_RE`, and filters to same-domain only (so we
    don't return @somerandomvendor.com emails that show up in a company's
    cookie-banner footer).

    Returns a sorted list of unique emails. Empty list on any failure. Never
    raises — caller can treat empty as "no data found, skip Source 3".

    Bounded to ~5 HTTP calls × ~5s timeout each = ~25s worst case per company.
    """
    from execution.extract_contacts import _EMAIL_RE
    from execution.web_search import fetch_page_text

    if not domain or not isinstance(domain, str):
        return []
    domain = domain.lower().strip().rstrip("/")
    if not domain or "/" in domain or "." not in domain:
        return []

    found: set[str] = set()
    pages_tried = 0

    for path in _COMPANY_CONTACT_PATHS:
        if pages_tried >= _COMPANY_CONTACT_MAX_PAGES:
            break
        url = f"https://{domain}{path}"
        try:
            text = fetch_page_text(url, max_chars=8_000)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"  find_company_contact_emails: {url} fetch crashed: {type(exc).__name__}")
            continue
        pages_tried += 1
        if not text:
            continue
        for email in _EMAIL_RE.findall(text):
            email_lower = email.lower()
            # Same-domain filter: must end with `@<domain>` or `@something.<domain>`
            email_host = email_lower.split("@", 1)[-1] if "@" in email_lower else ""
            if email_host == domain or email_host.endswith("." + domain):
                found.add(email_lower)

    return sorted(found)


def _regex_only_extract(text: str, source_url: str, source_label: str) -> list[dict]:
    """Cheap fallback: pull every email-shaped string. Name/title left blank."""
    from execution.extract_contacts import _EMAIL_RE, _GENERIC_MAILBOX_PREFIXES
    seen = set()
    out: list[dict] = []
    for email in _EMAIL_RE.findall(text):
        lower = email.lower()
        # Prefer non-generic; we still capture generic in case nothing else exists
        is_generic = any(lower.startswith(prefix) for prefix in _GENERIC_MAILBOX_PREFIXES)
        if email in seen:
            continue
        seen.add(email)
        out.append({
            "name": None,
            "email": email,
            "title": None,
            "source_url": source_url,
            "source": source_label,
            "is_generic": is_generic,
        })
        if len(out) >= 6:
            break
    # Move non-generic to front
    out.sort(key=lambda d: d.get("is_generic", False))
    for d in out:
        d.pop("is_generic", None)
    return out
