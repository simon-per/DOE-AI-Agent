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
