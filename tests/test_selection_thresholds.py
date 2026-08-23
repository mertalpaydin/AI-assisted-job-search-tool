"""Tests for the per-role-family selection threshold.

One global threshold says every role family is equally wanted. They are not.
An entry-level AI engineering role is worth reading at a mediocre score because
it moves toward the target; a pure procurement role is a deliberate fallback
and has to be clearly good first.

The regression this guards: before per-family thresholds, family E roles like
"Graduate AI software engineer" scored 0.58 and were dropped by a flat 0.65
bar, while family F procurement roles clustered at exactly 0.65 and filled a
quarter of the list.
"""
from __future__ import annotations

import pytest

from job_search.ai.screener import threshold_for


class Criteria:
    """Stands in for ScreeningCriteriaConfig."""

    def __init__(self, overrides=None, base=0.65):
        self.min_cv_match_score = base
        self.min_cv_match_score_by_archetype = overrides or {}


TUNED = Criteria({"C": 0.55, "D": 0.55, "E": 0.55, "F": 0.70})
FLAT = Criteria()


class TestPerFamilyOverrides:
    @pytest.mark.parametrize("family,expected", [
        ("C", 0.55), ("D", 0.55), ("E", 0.55), ("F", 0.70),
    ])
    def test_listed_families_use_their_own_bar(self, family, expected):
        assert threshold_for(family, TUNED) == expected

    @pytest.mark.parametrize("family", ["A", "B"])
    def test_unlisted_families_use_the_global_bar(self, family):
        assert threshold_for(family, TUNED) == 0.65

    def test_case_and_whitespace_do_not_matter(self):
        """The screener may hand back "e" or " E " depending on the model."""
        assert threshold_for("e", TUNED) == 0.55
        assert threshold_for("  E  ", TUNED) == 0.55
        assert threshold_for("f", TUNED) == 0.70


class TestFallbacks:
    @pytest.mark.parametrize("value", ["none", "", None, "Z"])
    def test_unclassified_or_unknown_falls_back_to_global(self, value):
        """An unclassified job must never be held to a bar nobody chose for it."""
        assert threshold_for(value, TUNED) == 0.65

    @pytest.mark.parametrize("family", ["A", "C", "E", "F", "none", None])
    def test_empty_override_map_means_one_bar_for_everything(self, family):
        assert threshold_for(family, FLAT) == 0.65

    def test_missing_attribute_is_survivable(self):
        """Config loaded from an older file has no override field at all."""
        class Old:
            min_cv_match_score = 0.65
        assert threshold_for("E", Old()) == 0.65


class TestTheBehaviourThatChanged:
    def test_graduate_ai_role_now_survives(self):
        """0.58, family E: dropped under a flat 0.65, kept at 0.55."""
        score = 0.58
        assert score < threshold_for("E", FLAT)
        assert score >= threshold_for("E", TUNED)

    def test_threshold_hugging_procurement_role_now_drops(self):
        """0.65, family F: exactly on the old bar, below the new one."""
        score = 0.65
        assert score >= threshold_for("F", FLAT)
        assert score < threshold_for("F", TUNED)

    def test_target_families_are_unaffected(self):
        """A and B are already well targeted; the change must not move them."""
        for family in ("A", "B"):
            assert threshold_for(family, FLAT) == threshold_for(family, TUNED)
