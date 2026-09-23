"""
On-demand AI operations for external jobs:
- Screening (scoring, archetype, reasoning)
- Cover letter generation
"""
from __future__ import annotations

import json
import re
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger

from job_search.ai.prompt_manager import PromptManager
from job_search.core.config import Config, load_config, load_secrets
from job_search.utils.formatting import clean_cover_letter_text


def _get_gemini_client() -> genai.Client:
    """Get an authenticated Gemini client using the first key from config/.env."""
    keys = load_secrets().gemini_api_keys
    if not keys:
        raise ValueError("No Gemini API key configured in config/.env.")
    return genai.Client(api_key=keys[0])


def screen_external_job(
    job_dict: dict[str, Any],
    config: Config | None = None,
    config_path: str = "config/config.yaml",
) -> dict[str, Any]:
    """
    On-demand screening for an external job.
    Returns dict with cv_match_score, archetype, reasoning.
    """
    cfg = config or load_config(config_path)
    client = _get_gemini_client()
    prompts = PromptManager()

    system_prompt, user_prompt = prompts.format_screening_prompt(
        job_title=job_dict.get("title", ""),
        company_name=job_dict.get("company_name", ""),
        job_location=job_dict.get("location", ""),
        remote_allowed=False,
        job_description=job_dict.get("description", ""),
        employment_status="Full-time",
        experience_level="Mid-Senior",
    )

    model_name = cfg.screening.gemini.model
    logger.info("Screening external job #{} ('{}') with model {}...", job_dict.get("id"), job_dict.get("title"), model_name)

    response = client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=cfg.screening.gemini.temperature,
            response_mime_type="application/json",
        ),
    )

    raw_text = response.text or "{}"
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        # Fallback regex extraction
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
        else:
            raise ValueError(f"Could not parse JSON response from Gemini: {raw_text[:200]}")

    score = float(parsed.get("cv_match_score", 0.0))
    archetype = str(parsed.get("archetype", "none")).upper()
    reasoning = str(parsed.get("reasoning", ""))

    return {
        "cv_match_score": score,
        "archetype": archetype,
        "screening_reasoning": reasoning,
    }


def generate_external_cover_letter(
    job_dict: dict[str, Any],
    config: Config | None = None,
    config_path: str = "config/config.yaml",
) -> str:
    """
    On-demand cover letter generation for an external job.
    Returns clean cover letter text.
    """
    cfg = config or load_config(config_path)
    client = _get_gemini_client()
    prompts = PromptManager()

    archetype = job_dict.get("archetype") or "none"

    system_prompt, user_prompt = prompts.format_cover_letter_prompt(
        job_title=job_dict.get("title", ""),
        company_name=job_dict.get("company_name", ""),
        job_location=job_dict.get("location", ""),
        job_description=job_dict.get("description", ""),
        archetype=archetype,
    )

    model_name = cfg.cover_letter.model
    logger.info("Generating cover letter for external job #{} ('{}') with model {}...", job_dict.get("id"), job_dict.get("title"), model_name)

    response = client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=cfg.cover_letter.temperature,
            max_output_tokens=cfg.cover_letter.max_tokens,
        ),
    )

    raw_cl = response.text or ""
    return clean_cover_letter_text(raw_cl)

