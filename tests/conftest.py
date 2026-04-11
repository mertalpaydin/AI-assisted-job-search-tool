"""Shared pytest fixtures for the job-search test suite."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Minimal YAML fixtures written to tmp_path so tests are self-contained
# ---------------------------------------------------------------------------

MINIMAL_CONFIG_YAML = textwrap.dedent("""\
    search:
      keywords:
        - "Python Developer"
      locations:
        - geo_id: "102713980"
          name: "Frankfurt am Main"
      rate_limits:
        requests_per_minute: 30
        delay_between_requests: 2.0
        max_retries: 3
""")

MINIMAL_CV_YAML = textwrap.dedent("""\
    cv:
      personal_info:
        name: "Test User"
        email: "test@example.com"
        location: "Frankfurt, Germany"
      summary: "Experienced Python developer."
      skills:
        technical:
          - Python
          - FastAPI
        languages:
          - language: "English"
            level: "Fluent"
          - language: "German"
            level: "Basic"
      experience:
        - title: "Backend Developer"
          company: "Acme Corp"
          duration: "2020-2024"
          description: "Built REST APIs."
      education:
        - degree: "B.Sc. Computer Science"
          institution: "Test University"
          year: 2020
      preferences:
        desired_roles:
          - "Backend Developer"
        german_requirement: "Prefer low"
""")

MINIMAL_PROMPTS_YAML = textwrap.dedent("""\
    screening:
      system_prompt: "You are a screener."
      user_prompt_template: |
        CV: {cv_text}
        Title: {job_title}
        Company: {company_name}
        Location: {job_location}
        Remote: {remote_allowed}
        Description: {job_description}
    cover_letter:
      system_prompt: "You are a cover letter writer."
      user_prompt_template: |
        Summary: {cv_summary}
        Company: {company_name}
        Position: {job_title}
        Location: {job_location}
        Description: {job_description}
""")


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Write minimal YAML config files to a temp directory and return the path."""
    (tmp_path / "config.yaml").write_text(MINIMAL_CONFIG_YAML, encoding="utf-8")
    (tmp_path / "cv.yaml").write_text(MINIMAL_CV_YAML, encoding="utf-8")
    (tmp_path / "prompts.yaml").write_text(MINIMAL_PROMPTS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def db(tmp_path: Path):
    """Return a fresh DatabaseManager backed by a temp SQLite file."""
    from job_search.core.database import DatabaseManager

    return DatabaseManager(str(tmp_path / "test.db"))
