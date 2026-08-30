"""On-demand recruiter outreach messages.

A plain function rather than a worker: this is called straight from the Web UI
request handler with somebody watching the button, so it must return an answer
or an error in one round trip. Nothing here touches a queue, and no state
survives the call — the route saves the result.
"""
from __future__ import annotations

from google import genai
from google.genai import types as genai_types
from loguru import logger
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from job_search.ai.errors import RateLimitError, TemporaryError, classify_exception
from job_search.ai.prompt_manager import RECRUITER_MESSAGE_LENGTHS, PromptManager
from job_search.core.config import Config
from job_search.core.database import DatabaseManager
from job_search.utils.api_rotation import GeminiAPIRotator
from job_search.utils.formatting import normalize_message_text


class RecruiterMessageError(Exception):
    """Anything that stopped a message being produced, phrased for the UI."""


# How long to wait for a rate-limited key before giving up. Long enough to ride
# out a momentary burst, short enough that the browser is not left hanging.
_KEY_WAIT_SECONDS = 20.0


def _call_gemini(cfg, api_key: str, system_prompt: str, user_prompt: str) -> str:
    """One blocking Gemini call. Mirrors CoverLetterWorker._call_gemini."""
    client = genai.Client(api_key=api_key)
    tools = (
        [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        if cfg.use_search_grounding
        else None
    )
    response = client.models.generate_content(
        model=cfg.model,
        contents=user_prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_tokens,
            thinking_config=genai_types.ThinkingConfig(thinking_level="low"),
            tools=tools,
        ),
    )
    # Only MAX_TOKENS means the answer was cut off. Other finish reasons
    # (STOP, safety, grounding states) are not truncation.
    if response.candidates:
        finish = response.candidates[0].finish_reason
        if finish is not None and finish.name == "MAX_TOKENS":
            raise TemporaryError(
                "Message truncated (finish_reason=MAX_TOKENS). Increase "
                "recruiter_message.max_tokens in config.yaml."
            )
    return response.text


def generate_recruiter_message(
    config: Config,
    db: DatabaseManager,
    api_keys: list[str],
    job,
    length: str = "note",
    prompts: PromptManager | None = None,
) -> str:
    """Draft one recruiter message for ``job``. Raises RecruiterMessageError.

    ``job`` is any record carrying title / company_name / formattedLocation /
    description / archetype, which both SelectedJobRow and JobRow do.

    ``prompts`` is loaded here rather than by the caller so a missing or
    malformed prompts.yaml reaches the browser as a readable message instead
    of a traceback from inside the route.
    """
    if length not in RECRUITER_MESSAGE_LENGTHS:
        raise RecruiterMessageError(f"Unknown message length: {length!r}")
    if not api_keys:
        raise RecruiterMessageError(
            "No Gemini API keys configured. Set GEMINI_API_KEY_1 in config/.env."
        )

    cfg = config.recruiter_message
    rotator = GeminiAPIRotator(api_keys)

    if prompts is None:
        try:
            prompts = PromptManager()
        except Exception as exc:
            raise RecruiterMessageError(f"Could not load prompts: {exc}") from exc

    system, user = prompts.format_recruiter_message_prompt(
        job_title=getattr(job, "title", None) or "",
        company_name=getattr(job, "company_name", None),
        job_location=getattr(job, "formattedLocation", None),
        job_description=getattr(job, "description", None),
        archetype=getattr(job, "archetype", None),
        length=length,
    )

    text = ""
    try:
        for attempt in Retrying(
            stop=stop_after_attempt(max(1, cfg.max_retries)),
            wait=wait_exponential(multiplier=2, min=cfg.retry_delay, max=cfg.retry_delay * 4),
            retry=retry_if_exception_type((RateLimitError, TemporaryError)),
            reraise=True,
        ):
            with attempt:
                # Bounded: a person is watching this request, so a key that is
                # backing off must fail fast rather than hold the connection.
                key_idx, api_key = rotator.get_next_available_key(
                    timeout=_KEY_WAIT_SECONDS
                )
                try:
                    text = _call_gemini(cfg, api_key, system, user)
                    rotator.record_success(key_idx)
                    db.log_api_usage(key_idx, "recruiter_message", success=True)
                except Exception as exc:
                    classified = classify_exception(exc)
                    rotator.record_error(key_idx, type(classified).__name__)
                    db.log_api_usage(
                        key_idx, "recruiter_message", success=False,
                        error_type=type(classified).__name__,
                    )
                    raise classified from exc
    except Exception as exc:
        logger.error("Recruiter message failed for job {}: {}", job.job_id, exc)
        raise RecruiterMessageError(str(exc)) from exc

    text = normalize_message_text(text)
    if not text:
        raise RecruiterMessageError("The model returned an empty message.")

    # Refuse an overlong note rather than trimming it. A connection request
    # clipped mid-word is worse than a button you press again, and LinkedIn
    # would reject it anyway.
    limit = cfg.max_chars.get(length)
    if limit and len(text) > limit:
        raise RecruiterMessageError(
            f"Model returned {len(text)} characters, over the {limit}-character "
            f"limit for this form. Try again, or switch to the longer form."
        )

    logger.info(
        "Recruiter message generated for job {} ({}, {} chars)",
        job.job_id, length, len(text),
    )
    return text
