"""
Consolidated LLM client — single source of truth for all API calls.

Replaces duplicated _call_openrouter(), _call_gemini(), _call_llm() across
evaluate_jobs.py, generate_cover_letter.py, generate_cv.py, send_followups.py,
and web_search.py.

Usage:
    from execution.llm_client import call_llm, parse_json_response

    response, provider = call_llm(or_key, gem_key, prompt, max_tokens=500)
    data = parse_json_response(response)
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

# Model configuration — change once, applies everywhere
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3-235b-a22b-2507")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"


def get_openrouter_key() -> str | None:
    return os.getenv("OPEN_ROUTER_API_KEY")


def get_gemini_key() -> str | None:
    return os.getenv("GOOGLE_AI_STUDIO_API_KEY")


def call_openrouter(
    api_key: str,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 500,
    json_mode: bool = False,
) -> str:
    """Call OpenRouter API with retry and rate limit handling."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "DOE AI Job Application Agent",
    }
    payload = {
        "model": OPENROUTER_MODEL,
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
                log.warning(f"[openrouter] Rate limited. Waiting {wait:.1f}s (attempt {attempt + 1}/5)...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError:
            if attempt < 4 and resp.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("OpenRouter API failed after 5 retries")


def call_gemini(
    api_key: str,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 500,
    json_mode: bool = False,
) -> str:
    """Call Gemini API with retry and rate limit handling."""
    url = f"{GEMINI_URL}?key={api_key}"
    gen_config: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    for attempt in range(5):
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=60)
            if resp.status_code == 429:
                base_wait = min(2 ** attempt * 2, 120)
                wait = base_wait + random.uniform(0, base_wait * 0.3)
                log.warning(f"[gemini] Rate limited. Waiting {wait:.1f}s (attempt {attempt + 1}/5)...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except requests.exceptions.HTTPError:
            if attempt < 4 and resp.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("Gemini API failed after 5 retries")


def call_llm(
    openrouter_key: str | None,
    gemini_key: str | None,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 500,
    json_mode: bool = False,
) -> tuple[str, str]:
    """Call LLM with OpenRouter primary, Gemini fallback.

    Returns (response_text, provider_name).
    """
    if openrouter_key:
        try:
            result = call_openrouter(openrouter_key, prompt, temperature, max_tokens, json_mode)
            return result, "openrouter"
        except Exception as e:
            log.warning(f"[openrouter] Failed: {e}")
            if gemini_key:
                log.info("[fallback] Switching to Gemini...")
            else:
                raise
    if gemini_key:
        result = call_gemini(gemini_key, prompt, temperature, max_tokens, json_mode)
        return result, "gemini"
    raise RuntimeError("No API keys available")


def parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response. Handles code blocks and markdown wrappers."""
    cleaned = text.strip()
    # Remove markdown code block wrappers
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\n?```\s*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    return {}
