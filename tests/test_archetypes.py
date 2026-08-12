"""Tests for role-family (archetype) classification and prompt selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from job_search.ai.prompt_manager import PromptManager
from job_search.core.database import ARCHETYPE_LABELS, ARCHETYPES

ALL_FAMILIES = ("A", "B", "C", "D", "E", "F")


@pytest.fixture()
def pm(config_dir: Path) -> PromptManager:
    return PromptManager(
        prompts_path=str(config_dir / "prompts.yaml"),
        cv_path=str(config_dir / "cv.yaml"),
        draft_cover_letter_path=str(config_dir / "missing_draft.txt"),
    )


class TestArchetypeConstants:
    def test_every_archetype_has_a_label(self) -> None:
        assert set(ARCHETYPES) == set(ARCHETYPE_LABELS)

    def test_none_is_a_valid_archetype(self) -> None:
        assert "none" in ARCHETYPES


class TestGuidanceSelection:
    @pytest.mark.parametrize("family", ALL_FAMILIES)
    def test_only_the_matching_block_is_sent(self, pm: PromptManager, family: str) -> None:
        """The whole point: an F job must never see guidance written for A."""
        system, _ = pm.format_cover_letter_prompt("T", "C", "L", "D", archetype=family)
        expected = pm.archetype_guidance(family)
        assert expected in system
        for other in ALL_FAMILIES:
            if other != family:
                other_block = pm.archetype_guidance(other)
                assert other_block not in system

    def test_lowercase_archetype_is_accepted(self, pm: PromptManager) -> None:
        assert pm.archetype_guidance("f") == pm.archetype_guidance("F")

    @pytest.mark.parametrize("value", [None, "", "none", "NONE", "zzz", "  "])
    def test_unclassified_falls_back_to_the_none_block(
        self, pm: PromptManager, value: str | None
    ) -> None:
        assert pm.archetype_guidance(value) == pm.archetype_guidance("none")

    def test_none_block_is_real_content_not_empty(self, pm: PromptManager) -> None:
        """Cover letters for unclassified jobs must still get instructions."""
        assert pm.archetype_guidance(None).strip()

    def test_placeholder_is_always_substituted(self, pm: PromptManager) -> None:
        for value in ("A", "F", None):
            system, _ = pm.format_cover_letter_prompt("T", "C", "L", "D", archetype=value)
            assert "{archetype_guidance}" not in system

    def test_archetype_label_reaches_the_user_prompt(self, pm: PromptManager) -> None:
        _, user = pm.format_cover_letter_prompt("T", "C", "L", "D", archetype="A")
        assert "A -" in user

    def test_default_argument_keeps_old_callers_working(self, pm: PromptManager) -> None:
        system, user = pm.format_cover_letter_prompt("T", "C", "L", "D")
        assert system and user


class TestScreenerArchetypeParsing:
    """_apply_criteria must never let an unrecognised label reach the database."""

    @staticmethod
    def _apply(raw: dict):
        from job_search.ai.screener import _apply_criteria
        from job_search.core.config import Config

        cfg = Config.model_validate({
            "search": {
                "keywords": ["x"],
                "locations": [{"geo_id": "1", "name": "X"}],
            },
        })
        return _apply_criteria(raw, cfg)

    @pytest.mark.parametrize("value,expected", [
        ("A", "A"), ("a", "A"), ("F", "F"), ("f", "F"),
        ("A. Procurement x AI", "A"),
        ("B) AI Transformation", "B"),
        ("none", "none"), ("NONE", "none"), ("", "none"), ("zzz", "none"), (None, "none"),
        ("Family A", "none"), ("G", "none"), ("AB", "none"),
    ])
    def test_archetype_is_normalised(self, value, expected) -> None:
        result = self._apply({
            "cv_match_score": 0.9,
            "german_requirement_level": "none",
            "archetype": value,
            "reasoning": "r",
        })
        assert result.archetype == expected

    def test_missing_archetype_key_defaults_to_none(self) -> None:
        result = self._apply({"cv_match_score": 0.9, "german_requirement_level": "none"})
        assert result.archetype == "none"

    def test_archetype_never_escapes_the_allowed_set(self) -> None:
        for value in ("G", "1", "AB", "<script>", "Family A", "Archetype"):
            result = self._apply({
                "cv_match_score": 0.5,
                "german_requirement_level": "none",
                "archetype": value,
            })
            assert result.archetype in ARCHETYPES
