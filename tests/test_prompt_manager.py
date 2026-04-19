"""Tests for job_search.ai.prompt_manager — PromptManager."""
from __future__ import annotations

from pathlib import Path

import pytest

from job_search.ai.prompt_manager import PromptManager


@pytest.fixture()
def pm(config_dir: Path) -> PromptManager:
    return PromptManager(
        prompts_path=str(config_dir / "prompts.yaml"),
        cv_path=str(config_dir / "cv.yaml"),
    )


class TestPromptManagerInit:
    def test_loads_without_error(self, pm: PromptManager) -> None:
        assert pm is not None

    def test_raises_on_missing_prompts_file(self, config_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            PromptManager(
                prompts_path=str(config_dir / "no_such.yaml"),
                cv_path=str(config_dir / "cv.yaml"),
            )

    def test_raises_on_missing_cv_file(self, config_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            PromptManager(
                prompts_path=str(config_dir / "prompts.yaml"),
                cv_path=str(config_dir / "no_such.yaml"),
            )


class TestCvTextRendering:
    def test_cv_text_contains_name(self, pm: PromptManager) -> None:
        assert "Test User" in pm.cv_text

    def test_cv_text_contains_location(self, pm: PromptManager) -> None:
        assert "Frankfurt" in pm.cv_text

    def test_cv_text_contains_summary(self, pm: PromptManager) -> None:
        assert "Experienced Python developer" in pm.cv_text

    def test_cv_text_contains_technical_skills(self, pm: PromptManager) -> None:
        assert "Python" in pm.cv_text
        assert "FastAPI" in pm.cv_text

    def test_cv_text_contains_languages(self, pm: PromptManager) -> None:
        assert "English" in pm.cv_text
        assert "German" in pm.cv_text

    def test_cv_text_contains_experience(self, pm: PromptManager) -> None:
        assert "Backend Developer" in pm.cv_text
        assert "Acme Corp" in pm.cv_text

    def test_cv_text_contains_education(self, pm: PromptManager) -> None:
        assert "B.Sc. Computer Science" in pm.cv_text
        assert "Test University" in pm.cv_text

    def test_cv_summary_contains_name_and_skills(self, pm: PromptManager) -> None:
        summary = pm.cv_summary
        assert "Test User" in summary
        assert "Python" in summary


class TestScreeningPrompt:
    def test_returns_two_strings(self, pm: PromptManager) -> None:
        system, user = pm.format_screening_prompt(
            job_title="Python Engineer",
            company_name="Acme",
            job_location="Frankfurt",
            remote_allowed=True,
            job_description="Build APIs.",
        )
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str) and len(user) > 0

    def test_user_prompt_contains_job_title(self, pm: PromptManager) -> None:
        _, user = pm.format_screening_prompt(
            "Data Scientist", "Corp", "Berlin", False, "Analyse data."
        )
        assert "Data Scientist" in user

    def test_user_prompt_contains_cv_text(self, pm: PromptManager) -> None:
        _, user = pm.format_screening_prompt("Dev", None, None, None, None)
        assert "Test User" in user  # cv_text injected

    def test_handles_none_fields_gracefully(self, pm: PromptManager) -> None:
        system, user = pm.format_screening_prompt(None, None, None, None, None)
        assert "Unknown" in user  # falls back to 'Unknown'

    def test_remote_allowed_renders(self, pm: PromptManager) -> None:
        _, user_yes = pm.format_screening_prompt("Dev", "Co", "loc", True, "desc")
        _, user_no = pm.format_screening_prompt("Dev", "Co", "loc", False, "desc")
        assert "Yes" in user_yes
        assert "No" in user_no

    def test_description_truncated_to_3000_chars(self, pm: PromptManager) -> None:
        long_desc = "x" * 5000
        _, user = pm.format_screening_prompt("Dev", "Co", "loc", True, long_desc)
        assert "x" * 3001 not in user


class TestCoverLetterPrompt:
    def test_returns_two_strings(self, pm: PromptManager) -> None:
        system, user = pm.format_cover_letter_prompt(
            job_title="Backend Engineer",
            company_name="StartupCo",
            job_location="Remote",
            job_description="Build microservices.",
        )
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str) and len(user) > 0

    def test_user_prompt_contains_company(self, pm: PromptManager) -> None:
        _, user = pm.format_cover_letter_prompt("Dev", "BigCorp", "Remote", "desc")
        assert "BigCorp" in user

    def test_handles_none_fields_gracefully(self, pm: PromptManager) -> None:
        system, user = pm.format_cover_letter_prompt(None, None, None, None)
        assert "Unknown" in user

    def test_full_description_passed_to_gemini(self, pm: PromptManager) -> None:
        """Cover letter prompt passes the full description — no truncation (Gemini has large context)."""
        long_desc = "y" * 5000
        _, user = pm.format_cover_letter_prompt("Dev", "Co", "loc", long_desc)
        assert "y" * 5000 in user
