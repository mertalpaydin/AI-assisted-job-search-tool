from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


# Expanded into the cover letter prompt so the model does not have to infer
# what a bare letter means. Keys mirror ARCHETYPES in core.database.
_ARCHETYPE_DESCRIPTIONS: dict[str, str] = {
    "A": "A - procurement / supply chain meets AI and analytics",
    "B": "B - AI transformation, enablement, adoption and strategy",
    "C": "C - AI / data product, delivery and solution consulting",
    "D": "D - applied data science with a domain angle",
    "E": "E - generic AI / ML / data engineering",
    "F": "F - pure procurement, sourcing or supply chain",
    "none": "not classified",
}


class PromptManager:
    """Loads prompts.yaml and cv.yaml, formats prompts for screening and cover letters."""

    def __init__(
        self,
        prompts_path: str = "config/prompts.yaml",
        cv_path: str = "config/cv.yaml",
        draft_cover_letter_path: str = "config/cover_letter_draft.txt",
        narrative_path: str = "config/narrative.yaml",
    ) -> None:
        self._prompts = self._load_yaml(prompts_path)
        self._cv = self._load_yaml(cv_path)
        self._narrative = self._load_optional_yaml(narrative_path)
        self._cv_text = self._render_cv_text()
        self._draft_cover_letter = self._load_draft(draft_cover_letter_path)

    @staticmethod
    def _load_draft(path: str) -> str:
        p = Path(path)
        if not p.exists():
            return "(No draft cover letter provided.)"
        return p.read_text(encoding="utf-8").strip()

    @staticmethod
    def _load_optional_yaml(path: str) -> dict[str, Any]:
        """Load a YAML file if present, otherwise return an empty mapping.

        The narrative file is optional: without it the cover letter prompt
        simply carries no story material and falls back to CV facts.
        """
        p = Path(path)
        if not p.exists():
            return {}
        with p.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def render_narrative(self, archetype: str | None = None) -> str:
        """Render the story material relevant to one role family.

        cv.yaml supplies the facts; this supplies the reasoning, obstacles and
        outcomes behind them. Proof points are tagged by archetype so a family D
        job never sees the procurement stories and vice versa.
        """
        data = (self._narrative or {}).get("narrative", {})
        if not data:
            return ""

        key = self._archetype_key(archetype)
        lines: list[str] = []

        pos = data.get("positioning", {})
        if pos:
            lines.append("POSITIONING")
            for field in ("one_liner", "what_i_want", "why_the_pivot", "risk_taken"):
                text = (pos.get(field) or "").strip()
                if text:
                    lines.append(f"- {field.replace('_', ' ')}: {text}")

        fit = data.get("company_fit", {})
        if fit:
            lines.append("\nWHAT ATTRACTS ME TO AN EMPLOYER (use for the company paragraph)")
            for item in fit.get("what_attracts_me", []):
                lines.append(f"- {item.strip()}")
            for item in fit.get("dealbreakers", []):
                lines.append(f"- avoid: {item.strip()}")

        # "none" gets everything, since we cannot tell which story fits.
        points = [
            pt for pt in data.get("proof_points", [])
            if key == "none" or key in [a.upper() for a in pt.get("archetypes", [])]
        ]
        if points:
            lines.append("\nPROOF POINTS (the source for anything the CV cannot say)")
            for pt in points:
                lines.append(f"\n### {pt.get('headline', pt.get('id', ''))}")
                for field in ("situation", "decision", "obstacle", "outcome", "transferable"):
                    text = (pt.get(field) or "").strip()
                    if text:
                        lines.append(f"{field.upper()}: {text}")

        themes = data.get("themes", [])
        if themes:
            lines.append("\nRECURRING THEMES (show through a proof point, never claim outright)")
            lines.extend(f"- {t}" for t in themes)

        return "\n".join(lines).strip()

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

        projects = cv.get("projects", [])
        if projects:
            lines.append("\nProjects:")
            for proj in projects:
                name = proj.get("name", "")
                desc = (proj.get("description") or "").strip()
                lines.append(f"- {name}: {desc}" if desc else f"- {name}")

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
        projects = ", ".join(p.get("name", "") for p in cv.get("projects", []))
        out = f"{info.get('name', '')} - {summary}\nKey skills: {skills}"
        if projects:
            out += f"\nProjects: {projects}"
        return out

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

    @staticmethod
    def _archetype_key(archetype: str | None) -> str:
        """Normalise a screener archetype to a guidance key ("A".."F" or "none")."""
        key = (archetype or "").strip().upper()
        return key if key in _ARCHETYPE_DESCRIPTIONS and key != "NONE" else "none"

    def archetype_guidance(self, archetype: str | None) -> str:
        """Return the cover letter guidance block for a single role family.

        Only this block reaches the model. Falls back to the "none" block when
        the job was never classified or carries an unknown family.
        """
        cfg = self._prompts.get("cover_letter", {})
        blocks = cfg.get("archetype_guidance") or {}
        key = self._archetype_key(archetype)
        return str(blocks.get(key) or blocks.get("none") or "").rstrip()

    def format_cover_letter_prompt(
        self,
        job_title: str,
        company_name: str | None,
        job_location: str | None,
        job_description: str | None,
        archetype: str | None = None,
    ) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for cover letter generation.

        The system prompt is the shared instruction set with the one guidance
        block for this job's role family substituted in. Guidance written for
        the other families is never sent.
        """
        cfg = self._prompts["cover_letter"]
        key = self._archetype_key(archetype)

        # Plain replace rather than str.format: the system prompt contains
        # literal bracketed [Review: ...] markers and other punctuation that
        # format() would misread.
        system = cfg["system_prompt"].replace(
            "{archetype_guidance}", self.archetype_guidance(archetype)
        ).strip()

        user = cfg["user_prompt_template"].format(
            cv_text=self._escape(self._cv_text),
            draft_cover_letter=self._escape(self._draft_cover_letter),
            job_title=self._escape(job_title or ""),
            company_name=self._escape(company_name or "Unknown"),
            job_location=self._escape(job_location or "Unknown"),
            job_description=self._escape(job_description or ""),
            archetype=self._escape(_ARCHETYPE_DESCRIPTIONS[key]),
            narrative=self._escape(self.render_narrative(archetype)
                                   or "(No narrative material provided.)"),
        )
        return system, user
