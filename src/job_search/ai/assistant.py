"""On-demand AI Application Assistant.

Provides interactive, context-aware assistance on job detail pages to answer
application form questions, draft custom responses, and analyze fit.
Conversations are persisted in lightweight JSON files on disk
(data/chats/{job_id}.json).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
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
from job_search.core.config import Config
from job_search.utils.api_rotation import GeminiAPIRotator


class AssistantError(Exception):
    """Errors encountered while answering assistant questions."""


_KEY_WAIT_SECONDS = 20.0


def load_chat(file_path: str | Path | None) -> list[dict[str, Any]]:
    """Load conversation messages from a JSON file."""
    if not file_path:
        return []
    p = Path(file_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("messages", [])
    except Exception as exc:
        logger.warning("Could not read chat file {}: {}", p, exc)
        return []


def save_chat(
    job_id: int,
    messages: list[dict[str, Any]],
    chat_dir: str | Path = "data/chats",
) -> str:
    """Save conversation messages to a JSON file and return relative path."""
    d = Path(chat_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{job_id}.json"

    payload = {
        "job_id": job_id,
        "updated_at": datetime.now().astimezone().isoformat(),
        "messages": messages,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(target).replace("\\", "/")


def delete_chat(file_path: str | Path | None) -> None:
    """Delete a chat file from disk if it exists."""
    if not file_path:
        return
    p = Path(file_path)
    if p.exists():
        try:
            p.unlink()
        except OSError as exc:
            logger.warning("Could not delete chat file {}: {}", p, exc)


def build_assistant_context(
    job: Any,
    cv_data: dict[str, Any] | None = None,
    cover_letter_text: str | None = None,
    narrative_data: dict[str, Any] | None = None,
    selected_contexts: list[str] | None = None,
) -> str:
    """Format only the selected context items into structured markdown."""
    selected = set(selected_contexts if selected_contexts is not None else ["job_description", "cv"])
    blocks = []

    if "job_description" in selected:
        jd_lines = [
            "### Job Information:",
            f"- Title: {getattr(job, 'title', None) or 'Unknown'}",
            f"- Company: {getattr(job, 'company_name', None) or 'Unknown'}",
            f"- Location: {getattr(job, 'formattedLocation', None) or 'Unknown'}",
        ]
        desc = getattr(job, "description", None)
        if desc:
            jd_lines.extend(["", "### Job Description:", desc.strip()])
        blocks.append("\n".join(jd_lines))

    if "cv" in selected and cv_data:
        blocks.append(
            "### Candidate CV:\n" + yaml.dump(cv_data, sort_keys=False, allow_unicode=True)
        )

    if "cover_letter" in selected and cover_letter_text:
        blocks.append("### Generated Cover Letter for this Job:\n" + cover_letter_text.strip())

    if "screening_reasoning" in selected:
        score = getattr(job, "cv_match_score", None)
        pct = f"{int(score * 100)}%" if score is not None else "N/A"
        reasoning = getattr(job, "screening_reasoning", None) or "No assessment available."
        arch = getattr(job, "archetype", None) or "Unclassified"
        blocks.append(
            f"### AI Screening Assessment:\n"
            f"- Match Score: {pct}\n"
            f"- Role Family Archetype: {arch}\n"
            f"- Reasoning: {reasoning}"
        )

    if "narrative" in selected and narrative_data:
        blocks.append(
            "### Candidate Career Narrative & Proof Points:\n"
            + yaml.dump(narrative_data, sort_keys=False, allow_unicode=True)
        )

    if not blocks:
        return "No specific context provided."

    return "\n\n---\n\n".join(blocks)


def _call_gemini(
    cfg,
    api_key: str,
    system_instruction: str,
    contents: list[genai_types.Content],
) -> str:
    """Execute one blocking Gemini call with multi-turn content."""
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=cfg.model,
        contents=contents,
        config=genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=cfg.temperature,
            max_output_tokens=cfg.max_tokens,
            thinking_config=genai_types.ThinkingConfig(thinking_level="low"),
        ),
    )
    if response.candidates:
        finish = response.candidates[0].finish_reason
        if finish is not None and finish.name == "MAX_TOKENS":
            raise TemporaryError("Response was truncated due to max token limits.")
    return response.text or ""


def ask_assistant(
    config: Config,
    api_keys: list[str],
    job: Any,
    message: str,
    context_types: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    cv_data: dict[str, Any] | None = None,
    cover_letter_text: str | None = None,
    narrative_data: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Process a user query with context and history, returning (reply, updated_messages)."""
    if not message or not message.strip():
        raise AssistantError("Message cannot be empty.")
    if not api_keys:
        raise AssistantError("No Gemini API keys configured.")

    cfg = config.assistant
    rotator = GeminiAPIRotator(api_keys)

    context_md = build_assistant_context(
        job=job,
        cv_data=cv_data,
        cover_letter_text=cover_letter_text,
        narrative_data=narrative_data,
        selected_contexts=context_types,
    )

    system_instruction = (
        "You are an expert career assistant helping the candidate craft truthful, compelling, "
        "and tailored answers to job application form questions and employer inquiries.\n\n"
        "Guidelines:\n"
        "1. Base all facts strictly on the candidate's provided CV, career narrative, and cover letter. "
        "Do not fabricate experience, credentials, skills, or achievements.\n"
        "2. Directly and concisely address the user's prompt or the application form question. "
        "If the user specifies a word or character limit, adhere to it strictly.\n"
        "3. Maintain a professional, confident, and authentic tone appropriate for job applications.\n"
        "4. Emphasize concrete achievements, relevant skills, and specific impact matching the job requirements.\n\n"
        "### Available Context:\n"
        f"{context_md}"
    )

    # Build multi-turn contents
    contents: list[genai_types.Content] = []
    messages = list(history or [])

    for past_msg in messages:
        role = "user" if past_msg.get("role") == "user" else "model"
        content_text = past_msg.get("content", "")
        if content_text:
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part.from_text(text=content_text)],
                )
            )

    # Current user message
    contents.append(
        genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=message.strip())],
        )
    )

    retrier = Retrying(
        retry=retry_if_exception_type((RateLimitError, TemporaryError)),
        stop=stop_after_attempt(len(api_keys) * 2 + 1),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )

    reply = ""
    try:
        for attempt in retrier:
            with attempt:
                key_idx, api_key = rotator.get_next_available_key(timeout=_KEY_WAIT_SECONDS)
                try:
                    reply = _call_gemini(cfg, api_key, system_instruction, contents)
                    rotator.record_success(key_idx)
                except Exception as exc:
                    err = classify_exception(exc)
                    rotator.record_error(key_idx, type(err).__name__)
                    raise err
    except RateLimitError as exc:
        raise AssistantError("All Gemini API keys are currently rate-limited.") from exc
    except TemporaryError as exc:
        raise AssistantError(f"Gemini API temporary error: {exc}") from exc
    except Exception as exc:
        logger.exception("Assistant call failed: {}", exc)
        raise AssistantError(f"Assistant request failed: {exc}") from exc

    # Append to conversation history
    now_iso = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    messages.append({
        "role": "user",
        "content": message.strip(),
        "context_used": context_types or ["job_description", "cv"],
        "timestamp": now_iso,
    })
    messages.append({
        "role": "model",
        "content": reply.strip(),
        "timestamp": now_iso,
    })

    return reply.strip(), messages
