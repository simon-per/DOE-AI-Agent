"""
Consolidated LLM client — single source of truth for all API calls.

Per-stage routing (set up 2026-05-03):
  - stage="cover_letter" → MODEL_COVER_LETTER (gemini-3-flash-preview)
  - stage="cheap"  (default) → MODEL_CHEAP (deepseek-v4-flash)
  - both fall back to MODEL_FALLBACK (qwen3-235b-a22b-2507) on primary failure

Everything goes through OpenRouter. The legacy Google-AI-Studio direct path
was removed — its free tier is too unreliable to rely on, and OpenRouter
gives us unified billing for all three models.

Usage:
    from execution.llm_client import call_llm, parse_json_response

    # Cheap stage (default) — deepseek primary, qwen fallback
    response, provider = call_llm(or_key, None, prompt, max_tokens=500)

    # Cover letter stage — gemini-3-flash primary, qwen fallback
    response, provider = call_llm(or_key, None, prompt, stage="cover_letter")
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

import requests
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-stage models — verified against https://openrouter.ai/api/v1/models on 2026-05-03.
# Override per-stage via env var without code changes.
MODEL_COVER_LETTER = os.getenv("MODEL_COVER_LETTER", "google/gemini-3-flash-preview")
MODEL_CHEAP        = os.getenv("MODEL_CHEAP",        "deepseek/deepseek-v4-flash")
MODEL_FALLBACK     = os.getenv("MODEL_FALLBACK",     "qwen/qwen3-235b-a22b-2507")

# Back-compat alias — evaluate_jobs.py logs this in its startup banner. New
# code should import the named constants above instead.
OPENROUTER_MODEL = MODEL_CHEAP


def get_openrouter_key() -> str | None:
    return os.getenv("OPEN_ROUTER_API_KEY")


def get_gemini_key() -> str | None:
    """Deprecated stub — Gemini is now accessed via OpenRouter.

    Kept so existing call sites that pass the result through to call_llm()
    don't break. Always returns None; the param is ignored downstream.
    """
    return None


def call_openrouter(
    api_key: str,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 500,
    json_mode: bool = False,
    model: str | None = None,
) -> str:
    """Call OpenRouter API with retry and rate limit handling.

    `model` defaults to MODEL_CHEAP when omitted. Pass an explicit slug
    (e.g. MODEL_COVER_LETTER, MODEL_FALLBACK) to route to a specific model.
    """
    chosen_model = model or MODEL_CHEAP
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "DOE AI Job Application Agent",
    }
    payload = {
        "model": chosen_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(5):
        try:
            resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
            if resp.status_code == 429:
                if "Retry-After" in resp.headers:
                    wait = int(resp.headers["Retry-After"]) + random.uniform(1, 3)
                else:
                    base_wait = min(2 ** attempt * 2, 120)
                    wait = base_wait + random.uniform(0, base_wait * 0.3)
                log.warning(f"[openrouter:{chosen_model}] Rate limited. Waiting {wait:.1f}s (attempt {attempt + 1}/5)...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            # Some routes (e.g. DeepSeek-v4-flash, a thinking model) return null
            # content when max_tokens is exhausted by reasoning. Treat that as an
            # empty response rather than crashing on None.strip().
            content = resp.json()["choices"][0]["message"].get("content") or ""
            return content.strip()
        except requests.exceptions.HTTPError:
            if attempt < 4 and resp.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError(f"OpenRouter API ({chosen_model}) failed after 5 retries")


def call_llm(
    openrouter_key: str | None,
    gemini_key: str | None = None,  # back-compat: ignored
    prompt: str = "",
    temperature: float = 0.3,
    max_tokens: int = 500,
    json_mode: bool = False,
    stage: str = "cheap",
) -> tuple[str, str]:
    """Call OpenRouter with stage-appropriate model, fall back to Qwen on failure.

    `stage`:
      - "cover_letter" → MODEL_COVER_LETTER primary
      - "cheap" (default) → MODEL_CHEAP primary

    `gemini_key` is accepted for backward compatibility with existing call
    sites but is no longer used — Gemini is reached via OpenRouter.

    Returns (response_text, provider_name) where provider_name is the
    OpenRouter slug actually used (so callers can log which model fired).
    """
    if not openrouter_key:
        raise RuntimeError(
            "OPEN_ROUTER_API_KEY not set — Gemini-via-Google-AI-Studio direct "
            "path was removed in the 2026-05-03 routing refactor."
        )

    primary = MODEL_COVER_LETTER if stage == "cover_letter" else MODEL_CHEAP

    try:
        result = call_openrouter(
            openrouter_key, prompt, temperature, max_tokens, json_mode, model=primary,
        )
        return result, primary
    except Exception as exc:
        if primary == MODEL_FALLBACK:
            # Already on the fallback — nothing left to try
            raise
        log.warning(f"[openrouter:{primary}] failed ({type(exc).__name__}: {exc}). "
                    f"Retrying with fallback {MODEL_FALLBACK}...")

    result = call_openrouter(
        openrouter_key, prompt, temperature, max_tokens, json_mode, model=MODEL_FALLBACK,
    )
    return result, MODEL_FALLBACK


def parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response. Handles code blocks and markdown wrappers."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}
