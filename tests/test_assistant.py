"""Unit tests for the on-demand AI Application Assistant."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from job_search.ai.assistant import (
    AssistantError,
    ask_assistant,
    build_assistant_context,
    delete_chat,
    load_chat,
    save_chat,
)
from job_search.core.config import Config, SearchConfig
from job_search.core.database import SelectedJobRow


@pytest.fixture
def dummy_job() -> SelectedJobRow:
    return SelectedJobRow(
        job_id=12345,
        title="AI Engineer",
        company_name="TechCorp",
        formattedLocation="Frankfurt, Germany",
        jobPostingUrl="https://linkedin.com/jobs/view/12345",
        workRemoteAllowed=1,
        description="We are seeking an AI Engineer with Python and LLM experience.",
        application_status="pending",
        applied_at=None,
        cv_match_score=0.88,
        german_requirement_level="none",
        is_selected=1,
        screening_reasoning="Strong alignment with candidate background.",
        cover_letter_text="Dear Hiring Manager,\nI am writing to apply...",
        generation_date="2026-09-20 12:00:00",
        generation_status=1,
        archetype="A",
    )


@pytest.fixture
def minimal_config() -> Config:
    return Config.model_validate({
        "search": {
            "keywords": ["AI"],
            "locations": [{"geo_id": "1", "name": "Frankfurt"}],
        }
    })


def test_load_chat_empty_or_missing(tmp_path: Path) -> None:
    assert load_chat(None) == []
    assert load_chat(tmp_path / "non_existent.json") == []

    # Malformed file
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("invalid json", encoding="utf-8")
    assert load_chat(bad_file) == []


def test_save_load_and_delete_chat(tmp_path: Path) -> None:
    chat_dir = tmp_path / "chats"
    messages = [
        {"role": "user", "content": "What is the role?", "timestamp": "2026-09-21 10:00:00"},
        {"role": "model", "content": "It is an AI Engineer role.", "timestamp": "2026-09-21 10:00:05"},
    ]

    # Save
    rel_path = save_chat(12345, messages, chat_dir=chat_dir)
    assert Path(rel_path).exists()

    # Load
    loaded = load_chat(rel_path)
    assert len(loaded) == 2
    assert loaded[0]["content"] == "What is the role?"
    assert loaded[1]["content"] == "It is an AI Engineer role."

    # Delete
    delete_chat(rel_path)
    assert not Path(rel_path).exists()
    assert load_chat(rel_path) == []


def test_build_assistant_context(dummy_job: SelectedJobRow) -> None:
    cv_data = {"name": "Candidate", "skills": ["Python", "Machine Learning"]}
    narrative_data = {"story": "Experienced in AI transformations."}

    # Default contexts: job_description, cv
    ctx = build_assistant_context(
        job=dummy_job,
        cv_data=cv_data,
        cover_letter_text=dummy_job.cover_letter_text,
        narrative_data=narrative_data,
        selected_contexts=["job_description", "cv"],
    )
    assert "Job Information:" in ctx
    assert "AI Engineer" in ctx
    assert "Candidate CV:" in ctx
    assert "Machine Learning" in ctx
    assert "Generated Cover Letter" not in ctx
    assert "Candidate Career Narrative" not in ctx

    # All contexts
    ctx_all = build_assistant_context(
        job=dummy_job,
        cv_data=cv_data,
        cover_letter_text=dummy_job.cover_letter_text,
        narrative_data=narrative_data,
        selected_contexts=["job_description", "cv", "cover_letter", "screening_reasoning", "narrative"],
    )
    assert "Job Description:" in ctx_all
    assert "Candidate CV:" in ctx_all
    assert "Generated Cover Letter for this Job:" in ctx_all
    assert "AI Screening Assessment:" in ctx_all
    assert "Candidate Career Narrative & Proof Points:" in ctx_all

    # Empty context selection
    ctx_empty = build_assistant_context(job=dummy_job, selected_contexts=[])
    assert ctx_empty == "No specific context provided."


def test_ask_assistant_validation(minimal_config: Config, dummy_job: SelectedJobRow) -> None:
    # Empty message
    with pytest.raises(AssistantError, match="Message cannot be empty"):
        ask_assistant(minimal_config, ["key1"], dummy_job, "")

    # No API keys
    with pytest.raises(AssistantError, match="No Gemini API keys"):
        ask_assistant(minimal_config, [], dummy_job, "Hello")


def test_ask_assistant_success(minimal_config: Config, dummy_job: SelectedJobRow) -> None:
    mock_candidate = MagicMock()
    mock_candidate.finish_reason = None
    mock_response = MagicMock()
    mock_response.text = "You are a great fit because of your Python experience."
    mock_response.candidates = [mock_candidate]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    history = [
        {"role": "user", "content": "Prior question"},
        {"role": "model", "content": "Prior answer"},
    ]

    with patch("job_search.ai.assistant.genai.Client", return_value=mock_client):
        reply, updated_history = ask_assistant(
            config=minimal_config,
            api_keys=["test-key"],
            job=dummy_job,
            message="Why am I a good fit?",
            context_types=["job_description", "cv"],
            history=history,
            cv_data={"skills": ["Python"]},
        )

    assert reply == "You are a great fit because of your Python experience."
    # Prior 2 messages + 1 user message + 1 model reply = 4 messages
    assert len(updated_history) == 4
    assert updated_history[2]["role"] == "user"
    assert updated_history[2]["content"] == "Why am I a good fit?"
    assert updated_history[3]["role"] == "model"
    assert updated_history[3]["content"] == reply
