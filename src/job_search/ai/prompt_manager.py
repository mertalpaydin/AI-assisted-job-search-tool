from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class PromptManager:
    """Loads prompts.yaml and cv.yaml, formats prompts for screening and cover letters."""

    def __init__(
        self,
        prompts_path: str = "config/prompts.yaml",
        cv_path: str = "config/cv.yaml",
        draft_cover_letter_path: str = "config/cover_letter_draft.txt",
    ) -> None:
        self._prompts = self._load_yaml(prompts_path)
        self._cv = self._load_yaml(cv_path)
        self._cv_text = self._render_cv_text()
        self._draft_cover_letter = self._load_draft(draft_cover_letter_path)

    @staticmethod
    def _load_draft(path: str) -> str:
        p = Path(path)
        if not p.exists():
            return "(No draft cover letter provided.)"
        return p.read_text(encoding="utf-8").strip()

    @staticmethod
    def _load_yaml(path: str) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")
        with p.open(encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _render_cv_text(self) -> str:
        """Flatten cv.yaml into a plain-text representation for prompt injection."""
        cv = self._cv.get("cv", {})
        lines: list[str] = []

        info = cv.get("personal_info", {})
        if info.get("name"):
            lines.append(f"Name: {info['name']}")
        if info.get("location"):
            lines.append(f"Location: {info['location']}")

        if cv.get("summary"):
            lines.append(f"\nSummary:\n{cv['summary'].strip()}")

        skills = cv.get("skills", {})
        tech = skills.get("technical", [])
        if tech:
            lines.append(f"\nTechnical Skills: {', '.join(tech)}")

        langs = skills.get("languages", [])
        if langs:
            lang_str = ", ".join(f"{l['language']} ({l['level']})" for l in langs)
            lines.append(f"Languages: {lang_str}")

        for exp in cv.get("experience", []):
            lines.append(
                f"\nExperience: {exp.get('title')} at {exp.get('company')} "
                f"({exp.get('duration')})\n{exp.get('description', '').strip()}"
            )

        for edu in cv.get("education", []):
            lines.append(
                f"\nEducation: {edu.get('degree')} — {edu.get('institution')} ({edu.get('year')})"
            )

        prefs = cv.get("preferences", {})
        if prefs.get("desired_roles"):
            lines.append(f"\nDesired Roles: {', '.join(prefs['desired_roles'])}")
        if prefs.get("location_preference"):
            lines.append(f"Location Preference: {prefs['location_preference']}")
        if prefs.get("german_requirement"):
            lines.append(f"German Requirement Preference: {prefs['german_requirement']}")

        return "\n".join(lines)

    @property
    def cv_text(self) -> str:
        return self._cv_text

    @property
    def cv_summary(self) -> str:
        """Shorter CV summary for cover letter prompts."""
        cv = self._cv.get("cv", {})
        info = cv.get("personal_info", {})
        summary = cv.get("summary", "").strip()
        skills = ", ".join(cv.get("skills", {}).get("technical", []))
        return f"{info.get('name', '')} — {summary}\nKey skills: {skills}"

    def format_screening_prompt(
        self,
        job_title: str,
        company_name: str | None,
        job_location: str | None,
        remote_allowed: bool | None,
        job_description: str | None,
    ) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for the screening task."""
        cfg = self._prompts["screening"]
        system = cfg["system_prompt"].strip()
        user = cfg["user_prompt_template"].format(
            cv_text=self._cv_text,
            job_title=job_title or "",
            company_name=company_name or "Unknown",
            job_location=job_location or "Unknown",
            remote_allowed="Yes" if remote_allowed else "No",
            job_description=(job_description or ""),
        )
        return system, user

    @staticmethod
    def _escape(text: str) -> str:
        """Escape literal { and } in user-supplied text so str.format() won't choke on them."""
        return text.replace("{", "{{").replace("}", "}}")

    def format_cover_letter_prompt(
        self,
        job_title: str,
        company_name: str | None,
        job_location: str | None,
        job_description: str | None,
    ) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for cover letter generation."""
        cfg = self._prompts["cover_letter"]
        system = cfg["system_prompt"].strip()
        user = cfg["user_prompt_template"].format(
            cv_text=self._escape(self._cv_text),
            draft_cover_letter=self._escape(self._draft_cover_letter),
            job_title=self._escape(job_title or ""),
            company_name=self._escape(company_name or "Unknown"),
            job_location=self._escape(job_location or "Unknown"),
            job_description=self._escape(job_description or ""),
        )
        return system, user
