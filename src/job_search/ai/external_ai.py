"""
On-demand AI operations for external jobs:
- Screening (scoring, archetype, reasoning)
- Cover letter generation
- Interactive assistant chat
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger

from job_search.ai.prompt_manager import PromptManager
from job_search.core.config import Config, load_config
from job_search.utils.formatting import clean_cover_letter_text


def _get_gemini_client(config: Config) -> genai.Client:
    """Get authenticated Gemini client from config or environment."""
    keys = config.api_keys
    key = keys[0] if keys else os.getenv("GEMINI_API_KEY_3") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError("No Gemini API key configured in config/.env or environment.")
    return genai.Client(api_key=key)


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
    client = _get_gemini_client(cfg)
    prompts = PromptManager()

    system_prompt, user_prompt = prompts.format_screening_prompt(
        job_title=job_dict.get("title", ""),
        company_name=job_dict.get("company_name", ""),
        job_location=job_dict.get("location", ""),
        remote_allowed=False,
        employment_status="Full-time",
        experience_level="Mid-Senior",
        job_functions="",
        industries="",
        company_size="",
        job_description=job_dict.get("description", ""),
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
    client = _get_gemini_client(cfg)
    prompts = PromptManager()

    archetype = job_dict.get("archetype") or "none"

    system_prompt, user_prompt = prompts.format_cover_letter_prompt(
        job_title=job_dict.get("title", ""),
        company_name=job_dict.get("company_name", ""),
        job_location=job_dict.get("location", ""),
        job_description=job_dict.get("description", ""),
        archetype=archetype,
    )

    model_name = cfg.cover_letter.gemini.model
    logger.info("Generating cover letter for external job #{} ('{}') with model {}...", job_dict.get("id"), job_dict.get("title"), model_name)

    response = client.models.generate_content(
        model=model_name,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=cfg.cover_letter.gemini.temperature,
            max_output_tokens=cfg.cover_letter.gemini.max_tokens,
        ),
    )

    raw_cl = response.text or ""
    return clean_cover_letter_text(raw_cl)


def chat_external_job(
    job_dict: dict[str, Any],
    user_message: str,
    history: list[dict[str, str]],
    config: Config | None = None,
    config_path: str = "config/config.yaml",
) -> str:
    """
    Interactive AI assistant chat about an external job.
    """
    cfg = config or load_config(config_path)
    client = _get_gemini_client(cfg)
    prompts = PromptManager()

    cv_text = prompts._cv_text

    system_prompt = (
        "You are an expert career advisor and job search assistant. "
        "You have the candidate's CV and the job posting details. Answer the candidate's questions "
        "specifically, accurately, and honestly based on their background and this job.\n\n"
        f"--- CANDIDATE CV ---\n{cv_text}\n\n"
        f"--- JOB DETAILS ---\n"
        f"Title: {job_dict.get('title')}\n"
        f"Company: {job_dict.get('company_name')}\n"
        f"Location: {job_dict.get('location')}\n"
        f"Source: {job_dict.get('source')}\n"
        f"Description:\n{job_dict.get('description')}\n"
    )

    # Build conversation contents
    conversation = []
    for turn in history:
        role = "user" if turn.get("role") == "user" else "model"
        conversation.append(genai_types.Content(
            role=role,
            parts=[genai_types.Part.from_text(text=turn.get("content", ""))]
        ))

    conversation.append(genai_types.Content(
        role="user",
        parts=[genai_types.Part.from_text(text=user_message)]
    ))

    model_name = cfg.assistant.model if hasattr(cfg, "assistant") and cfg.assistant.model else "gemini-2.5-flash"

    response = client.models.generate_content(
        model=model_name,
        contents=conversation,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.4,
        ),
    )

    return response.text or "No response generated."
