"""
Stage 2: Evaluate scraped jobs for candidate fit.
Primary: OpenRouter (Deepseek-V3.2-Speciale) - improved reasoning, paid (low cost)
Fallback: Google AI Studio (Gemini 3 Flash) - free but rate-limited

Pre-filtering: Auto-rejects jobs requiring PhD, French language, or senior/executive level
Cost optimization: Reduces LLM API calls by 20-40% on average

Input:  .tmp/raw_jobs.json
Output: .tmp/scored_jobs.json

Usage:
    python execution/evaluate_jobs.py
    python execution/evaluate_jobs.py --limit 5   # test with 5 jobs
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TMP_DIR = PROJECT_ROOT / ".tmp"
INPUT_FILE = TMP_DIR / "raw_jobs.json"
OUTPUT_FILE = TMP_DIR / "scored_jobs.json"
CHECKPOINT_FILE = TMP_DIR / "evaluation_checkpoint.json"

# LLM client and profile from shared modules
from execution.llm_client import call_llm as _call_llm_shared, parse_json_response
from execution.profile_loader import load_profile as _load_profile

EVALUATION_PROMPT = """You are evaluating job fit for a candidate.

CANDIDATE PROFILE:
{profile}

JOB LISTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Score this job 1-10 for candidate fit. Consider:
1. Skills match (CRM, SAP, BI tools, SQL, CPQ, marketing automation)
2. Experience level match (junior/mid, ~3 years experience)
3. Language requirements vs candidate's languages (DE native, EN C1, IT medium)
4. Degree requirements — see rules below
5. Location in Switzerland (candidate is relocating)
6. Growth potential in AI, automation, data-driven areas

DEGREE RULES (important):
- Candidate has Matura + ~3 years hands-on CRM experience + professional certifications (Google, Microsoft).
- A Bachelor's degree ≈ 2-3 years of experience. Candidate's work + certs compensates.
- Scoring process for degrees:
  1. First evaluate the job based on skills/experience match (ignore degree)
  2. If Bachelor's is "preferred" or "nice-to-have": NO penalty
  3. If Bachelor's is REQUIRED (mandatory): subtract 1 point
  4. If Master's or PhD is required: subtract 3 points
  5. Keep final score within 1-10

The candidate needs ~100 applications for the Swiss job market, so score generously for any role where their skills are transferable. The question is: could this candidate reasonably succeed in this role?

Scoring guide with examples:
- 9-10: Perfect fit. Core skills directly match, no barriers.
  Example: "CRM Specialist / Dynamics 365 Administrator", no degree required → 9
- 7-8: Strong fit. Skills mostly align, any gaps are learnable on the job.
  Example: "Business Analyst CRM" needing SQL + CRM, Bachelor's preferred → 8
  Example: "Sales Operations & CRM Spezialist" with vague description but matching title → 8
- 6: Worth applying. Transferable skills exist, candidate could grow into it.
  Example: "Marketing Automation Manager" needing campaign experience → 6
  Example: "Digital Sales Consultant" with some CRM overlap → 6
- 4-5: Stretch. Only tangential skill overlap or significant experience gap.
  Example: "Sales Operations Manager" needing 5+ years experience → 5
  Example: "Energy Data Analyst" needing Python/Databricks — wrong domain, tool overlap (Power BI) is not enough → 4
- 2-3: Poor fit. Wrong domain or major skill mismatch.
  Example: "SAP ABAP Developer" needing coding expertise → 3
- 1: Completely wrong field.
  Example: "Java Backend Engineer" or "Head of Marketing" → 1

DOMAIN RULE: Only score 6+ if the role is in or adjacent to CRM, sales operations, marketing automation, or business analysis. Sharing a single tool (e.g. Power BI, SQL, Excel) with an unrelated domain (energy, finance, engineering, healthcare, logistics) is NOT enough — the core job function must relate to the candidate's experience.

CRITICAL: You MUST respond with ONLY valid JSON. No markdown, no code blocks, no other text.
Your entire response must be this exact JSON structure:
{{"score": 5, "reasoning": "Brief explanation here", "key_matches": ["match1", "match2"], "key_gaps": ["gap1", "gap2"], "degree_required": false, "languages_ok": true}}

Keep reasoning under 200 characters. Avoid quotes, newlines, and special characters in string values."""


def _call_llm(openrouter_key: str | None, gemini_key: str | None, prompt: str) -> str:
    """Call LLM via shared client. Uses temperature=0.1 and JSON mode for evaluation."""
    result, _ = _call_llm_shared(openrouter_key, gemini_key, prompt, temperature=0.1, max_tokens=500, json_mode=True)
    return result


def _get_profile(openrouter_key: str | None) -> str:
    """Return anonymized profile for OpenRouter (Deepseek), full for Gemini."""
    profile = _load_profile()
    if openrouter_key:
        return profile.profile_anonymous()
    return profile.profile_full()


    # _parse_json_response consolidated into execution/llm_client.py → parse_json_response
_parse_json_response = parse_json_response


def prefilter_job(job: dict) -> tuple[bool, str]:
    """
    Pre-filter jobs based on hard requirements that disqualify the candidate.
    Returns (should_evaluate, rejection_reason).

    Filters out:
    - PhD/Doctorate degree requirements
    - French language (when German/English not available)
    - Senior/Executive level positions
    - Pure software engineering roles
    - Internships/Praktikum
    - Off-domain industries (healthcare, manufacturing, legal/finance, trades)
    - No description available
    """
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()

    # Auto-reject PhD/doctorate requirements
    if "phd" in text or "ph.d" in text or "doctorate" in text or "doktor" in text:
        return (False, "Requires PhD/Doctorate")

    # Auto-reject jobs with no description (can't evaluate properly, wastes LLM credits)
    # If description_source is "fetched", the job now HAS a description — evaluate normally.
    # EXCEPTION: Allow Glassdoor jobs with desc_source=="empty" through (known jobspy limitation,
    # we tried to fetch but couldn't — evaluate on title+company only, less reliable).
    desc = job.get("description", "").strip()
    company = job.get("company", "").strip()
    source = job.get("source", "").lower()
    desc_source = job.get("description_source", "original")
    # [M4] If BOTH company and description are empty, evaluation is useless (title alone)
    if (not desc or desc == "nan") and not company:
        return (False, "No company or description available")
    if (not desc or desc == "nan"):
        # Glassdoor with empty desc_source: known limitation, allow through
        if "glassdoor" in source and desc_source == "empty":
            pass  # evaluate on title+company
        else:
            return (False, "No description available")

    # Auto-reject if French required AND German/English not mentioned
    has_french_req = (
        "französisch" in text or
        "french" in text or
        "français" in text or
        ("langue" in text and "français" in text)
    )
    has_german_ok = (
        "deutsch" in text or
        "german" in text or
        "allemand" in text
    )
    has_english_ok = (
        "english" in text or
        "englisch" in text or
        "anglais" in text
    )

    if has_french_req and not (has_german_ok or has_english_ok):
        return (False, "French required, no German/English")

    # Auto-reject senior/executive positions (candidate has 3 years experience)
    title_lower = job.get("title", "").lower()
    senior_keywords = [
        "senior manager", "director", "head of", "vp ", "vice president",
        "chief", "executive", "c-level", "leiter", "geschäftsführer"
    ]
    if any(kw in title_lower for kw in senior_keywords):
        return (False, "Senior/Executive level")

    # Auto-reject pure software engineering roles (wrong career track)
    dev_keywords = [
        "software engineer", "full stack", "java developer", "python developer",
        ".net developer", "frontend developer", "backend developer",
        "devops engineer", "data scientist", "machine learning engineer",
    ]
    if any(kw in title_lower for kw in dev_keywords):
        return (False, "Pure software engineering role")

    # Auto-reject internships (too junior, 3 years experience)
    if "praktikum" in title_lower or "internship" in title_lower or "intern " in title_lower:
        return (False, "Internship/Praktikum")

    # Auto-reject off-domain industries (title only — with broader search terms these show up)
    healthcare_keywords = [
        "pflegefachperson", "pflegefachfrau", "pflegefachmann", "krankenpflege",
        "arzt", "ärztin", "therapeut", "apotheker", "hebamme", "zahnarzt",
    ]
    if any(kw in title_lower for kw in healthcare_keywords):
        return (False, "Healthcare role")

    manufacturing_keywords = [
        "maschinenbauingenieur", "produktionsingenieur", "fertigungsleiter",
        "konstrukteur", "werkzeugmacher", "polymechaniker",
    ]
    if any(kw in title_lower for kw in manufacturing_keywords):
        return (False, "Manufacturing/engineering role")

    legal_finance_keywords = [
        "rechtsanwalt", "jurist", "notar", "treuhänder",
        "wirtschaftsprüfer", "steuerberater",
    ]
    if any(kw in title_lower for kw in legal_finance_keywords):
        return (False, "Legal/finance specialist role")

    trades_keywords = [
        "elektriker", "elektroinstallateur", "sanitärinstallateur",
        "schreiner", "maurer", "dachdecker", "gärtner",
    ]
    if any(kw in title_lower for kw in trades_keywords):
        return (False, "Trades/craft role")

    return (True, "")


def evaluate_job(openrouter_key: str | None, gemini_key: str | None, job: dict) -> dict:
    """Evaluate a single job listing against the candidate profile."""
    description = job.get("description", "")
    if not description or description.strip() == "":
        description = "(No description available)"

    # Truncate long descriptions (documented limit)
    if len(description) > 3000:
        log.info(f"Truncating description for '{job.get('title', '?')}' ({len(description)} -> 3000 chars)")
        description = description[:3000] + "..."

    prompt = EVALUATION_PROMPT.format(
        profile=_get_profile(openrouter_key),
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        location=job.get("location", "Unknown"),
        description=description,
    )

    # Retry up to 3 times on JSON parse failure per directive
    for json_attempt in range(3):
        try:
            text = _call_llm(openrouter_key, gemini_key, prompt)
            result = _parse_json_response(text)

            # Validate required fields exist and have correct types
            if "score" not in result:
                raise ValueError("Missing required field 'score'")
            score = int(result["score"])
            score = max(1, min(10, score))

            reasoning = result.get("reasoning", "")
            if not isinstance(reasoning, str):
                reasoning = str(reasoning)

            key_matches = result.get("key_matches", [])
            if not isinstance(key_matches, list):
                key_matches = [str(key_matches)] if key_matches else []

            key_gaps = result.get("key_gaps", [])
            if not isinstance(key_gaps, list):
                key_gaps = [str(key_gaps)] if key_gaps else []

            return {
                "score": score,
                "reasoning": reasoning,
                "key_matches": key_matches,
                "key_gaps": key_gaps,
                "degree_required": bool(result.get("degree_required", False)),
                "languages_ok": bool(result.get("languages_ok", True)),
            }

        except (json.JSONDecodeError, ValueError, TypeError) as e:
            if json_attempt < 2:
                log.warning(f"Response validation failed for '{job.get('title', '?')}': {e}, retrying ({json_attempt + 1}/3)...")
                time.sleep(1)
                continue
            log.warning(f"Response validation failed 3 times for '{job.get('title', '?')}': {e}")
            return _default_evaluation(f"Validation error after 3 retries: {e}")
        except Exception as e:
            log.warning(f"API error for '{job.get('title', '?')}': {e}")
            return _default_evaluation(str(e))

    return _default_evaluation("Unexpected failure")


def _default_evaluation(reason: str) -> dict:
    return {
        "score": 5,
        "reasoning": f"Evaluation failed (neutral score assigned): {reason}",
        "key_matches": [],
        "key_gaps": [],
        "degree_required": False,
        "languages_ok": True,
    }


def _load_checkpoint() -> set:
    """Load checkpoint of processed job IDs."""
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def _save_checkpoint(processed_ids: set):
    """Save checkpoint of processed job IDs."""
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(processed_ids), f)


def _get_job_id(job: dict) -> str:
    """Generate a unique ID for a job (includes URL hash to avoid collisions).

    [M5] Case-normalized to prevent duplicate evaluations for same job with different casing.
    """
    title = job.get("title", "Unknown").lower()
    company = job.get("company", "Unknown").lower()
    url = job.get("url", "")
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:8] if url else "nourl"
    return f"{company}_{title}_{url_hash}"


def main():
    parser = argparse.ArgumentParser(description="Evaluate scraped jobs for candidate fit")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of jobs to evaluate (0 = all)")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Clear checkpoint and re-evaluate all jobs")
    args = parser.parse_args()

    openrouter_key = os.getenv("OPEN_ROUTER_API_KEY")
    gemini_key = os.getenv("GOOGLE_AI_STUDIO_API_KEY")

    if not openrouter_key and not gemini_key:
        log.error("No API keys found. Set OPEN_ROUTER_API_KEY or GOOGLE_AI_STUDIO_API_KEY in .env")
        sys.exit(1)

    if openrouter_key:
        from execution.llm_client import MODEL_CHEAP, MODEL_FALLBACK
        log.info(f"Primary model: {MODEL_CHEAP} (OpenRouter)")
        log.info(f"Fallback model: {MODEL_FALLBACK} (OpenRouter)")

    if not INPUT_FILE.exists():
        log.error(f"Input file not found: {INPUT_FILE}")
        log.error("Run execution/scrape_jobs.py first (Stage 1)")
        sys.exit(1)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    log.info(f"Loaded {len(jobs)} jobs from {INPUT_FILE}")

    if args.limit > 0:
        jobs = jobs[: args.limit]
        log.info(f"Limiting evaluation to {len(jobs)} jobs")

    # Pre-filter jobs to save API costs
    log.info("Pre-filtering jobs based on hard requirements...")
    filtered_jobs = []
    rejected_jobs = []

    for job in jobs:
        should_eval, reason = prefilter_job(job)
        if should_eval:
            filtered_jobs.append(job)
        else:
            rejected_jobs.append({**job, "rejection_reason": reason})

    log.info(f"Pre-filter results: {len(filtered_jobs)} passed, {len(rejected_jobs)} rejected")
    if rejected_jobs:
        # Log rejection breakdown
        rejection_counts = {}
        for job in rejected_jobs:
            reason = job["rejection_reason"]
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        log.info(f"Rejection breakdown: {rejection_counts}")

    # Load checkpoint (if not reset)
    if args.reset_checkpoint:
        log.info("Resetting checkpoint (--reset-checkpoint flag)")
        processed_ids = set()
        if CHECKPOINT_FILE.exists():
            CHECKPOINT_FILE.unlink()
    else:
        processed_ids = _load_checkpoint()
        if processed_ids:
            log.info(f"Loaded checkpoint: {len(processed_ids)} jobs already processed")

    scored_jobs = []

    for i, job in enumerate(filtered_jobs):
        job_id = _get_job_id(job)

        # Skip if already processed
        if job_id in processed_ids:
            log.info(f"[{i + 1}/{len(filtered_jobs)}] Skipping (already processed): {job.get('title', 'Unknown')} at {job.get('company', 'Unknown')}")
            continue

        log.info(f"[{i + 1}/{len(filtered_jobs)}] Evaluating: {job.get('title', 'Unknown')} at {job.get('company', 'Unknown')}")

        evaluation = evaluate_job(openrouter_key, gemini_key, job)
        scored_job = {**job, **evaluation}
        scored_jobs.append(scored_job)

        # Mark as processed and save checkpoint
        processed_ids.add(job_id)
        _save_checkpoint(processed_ids)

        # Rate limit: 2s between calls (conservative, prevents rate limit errors)
        if i < len(filtered_jobs) - 1:
            time.sleep(2.0)

    # Add rejected jobs with score 0 (filtered out)
    for job in rejected_jobs:
        scored_jobs.append({
            **job,
            "score": 0,
            "reasoning": f"Pre-filtered: {job['rejection_reason']}",
            "key_matches": [],
            "key_gaps": [job["rejection_reason"]],
            "degree_required": "PhD" in job["rejection_reason"],
            "languages_ok": "French" not in job["rejection_reason"],
        })
        # Remove the rejection_reason key as it's now in reasoning
        del scored_jobs[-1]["rejection_reason"]

    # Sort by score descending
    scored_jobs.sort(key=lambda x: x["score"], reverse=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(scored_jobs, f, ensure_ascii=False, indent=2)

    log.info(f"Saved {len(scored_jobs)} scored jobs to {OUTPUT_FILE}")

    # Print summary
    if scored_jobs:
        # Calculate cost savings
        llm_calls = len(filtered_jobs)
        total_jobs = len(jobs)
        filtered_count = len(rejected_jobs)
        cost_savings_pct = (filtered_count / total_jobs * 100) if total_jobs > 0 else 0

        log.info(f"Cost optimization: {llm_calls} LLM calls (saved {filtered_count} calls, {cost_savings_pct:.1f}% reduction)")

        avg_score = sum(j["score"] for j in scored_jobs) / len(scored_jobs)
        top_5 = scored_jobs[:5]
        log.info(f"Average score: {avg_score:.1f}")
        log.info("Top 5 matches:")
        for j in top_5:
            log.info(f"  [{j['score']}/10] {j['title']} at {j['company']} ({j['location']})")

    return scored_jobs


if __name__ == "__main__":
    main()
