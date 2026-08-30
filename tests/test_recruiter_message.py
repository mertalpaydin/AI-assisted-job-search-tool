"""Tests for the on-demand recruiter message generator.

The provider call itself is stubbed. What is worth testing is everything
wrapped around it, because this is the one AI path with a person waiting on
the other end: the character ceilings are enforced, a blank answer is refused,
paragraph breaks survive, and no failure mode can hang the request.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from job_search.ai import recruiter_message as rm
from job_search.ai.prompt_manager import PromptManager
from job_search.core.config import Config
from job_search.core.database import DatabaseManager


@pytest.fixture()
def config() -> Config:
    return Config.model_validate({
        "search": {
            "keywords": ["AI Engineer"],
            "locations": [{"geo_id": "1", "name": "Frankfurt"}],
        },
    })


@pytest.fixture()
def prompts(config_dir: Path) -> PromptManager:
    return PromptManager(
        prompts_path=str(config_dir / "prompts.yaml"),
        cv_path=str(config_dir / "cv.yaml"),
    )


@pytest.fixture()
def job(db: DatabaseManager):
    db.insert_job(97001, "kw", "loc")
    db.update_job_details(97001, {
        "title": "AI Engineer", "company_name": "Acme",
        "formattedLocation": "Frankfurt", "description": "Build things.",
    })
    return db.get_selected_job(97001)


def _stub(monkeypatch, text: str) -> None:
    monkeypatch.setattr(rm, "_call_gemini", lambda cfg, key, system, user: text)


def _generate(config, db, prompts, job, length="note"):
    return rm.generate_recruiter_message(
        config=config, db=db, api_keys=["key-1"], job=job,
        length=length, prompts=prompts,
    )


def test_returns_the_message(config, db, prompts, job, monkeypatch) -> None:
    _stub(monkeypatch, "Hi, I saw the AI Engineer role and would like to talk.")
    assert _generate(config, db, prompts, job).startswith("Hi, I saw")


def test_logs_the_call_against_the_key(config, db, prompts, job, monkeypatch) -> None:
    """An on-demand call is billed like every other one, so it is accounted for."""
    _stub(monkeypatch, "Short and sweet.")
    _generate(config, db, prompts, job)
    with db._cursor() as cur:
        cur.execute("SELECT endpoint, success FROM api_usage")
        assert ("recruiter_message", 1) in [tuple(r) for r in cur.fetchall()]


def test_paragraph_breaks_survive(config, db, prompts, job, monkeypatch) -> None:
    """An InMail is read on a phone; the blank line is the only structure."""
    _stub(monkeypatch, "First thought.\n\nSecond thought.\n\n\n\nThird thought.")
    out = _generate(config, db, prompts, job, length="inmail")
    assert out == "First thought.\n\nSecond thought.\n\nThird thought."


def test_a_note_over_300_characters_is_refused(
    config, db, prompts, job, monkeypatch
) -> None:
    """LinkedIn will not send it, so returning it would only waste a paste."""
    _stub(monkeypatch, "x" * 350)
    with pytest.raises(rm.RecruiterMessageError, match="350 characters"):
        _generate(config, db, prompts, job, length="note")


def test_the_same_text_is_fine_as_an_inmail(
    config, db, prompts, job, monkeypatch
) -> None:
    _stub(monkeypatch, "x" * 350)
    assert len(_generate(config, db, prompts, job, length="inmail")) == 350


def test_an_empty_answer_is_refused(config, db, prompts, job, monkeypatch) -> None:
    _stub(monkeypatch, "   \n  ")
    with pytest.raises(rm.RecruiterMessageError, match="empty"):
        _generate(config, db, prompts, job)


def test_unknown_length_is_refused(config, db, prompts, job, monkeypatch) -> None:
    _stub(monkeypatch, "anything")
    with pytest.raises(rm.RecruiterMessageError, match="length"):
        _generate(config, db, prompts, job, length="telegram")


def test_missing_api_keys_is_a_readable_error(config, db, prompts, job) -> None:
    with pytest.raises(rm.RecruiterMessageError, match="GEMINI_API_KEY_1"):
        rm.generate_recruiter_message(
            config=config, db=db, api_keys=[], job=job, prompts=prompts,
        )


def test_a_provider_failure_becomes_a_recruiter_message_error(
    config, db, prompts, job, monkeypatch
) -> None:
    def _boom(cfg, key, system, user):
        raise RuntimeError("400 INVALID_ARGUMENT")

    monkeypatch.setattr(rm, "_call_gemini", _boom)
    with pytest.raises(rm.RecruiterMessageError, match="INVALID_ARGUMENT"):
        _generate(config, db, prompts, job)


def test_a_rate_limited_key_does_not_hang_the_request(
    config, db, prompts, job, monkeypatch
) -> None:
    """With one key, a 429 backs it off for 60s. The request must not wait."""
    monkeypatch.setattr(rm, "_KEY_WAIT_SECONDS", 0.0)
    config.recruiter_message.retry_delay = 0  # no need to sit through the backoff here

    calls = {"n": 0}

    def _rate_limited(cfg, key, system, user):
        calls["n"] += 1
        raise RuntimeError("429 quota exceeded")

    monkeypatch.setattr(rm, "_call_gemini", _rate_limited)
    with pytest.raises(rm.RecruiterMessageError):
        _generate(config, db, prompts, job)
    # First attempt reached the provider; the retry gave up on the key instead
    # of blocking out the backoff.
    assert calls["n"] == 1
