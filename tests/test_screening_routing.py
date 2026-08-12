"""Tests for screening mode routing.

The rule that matters: "auto" decides on who is waiting, not on how many jobs
are queued. A scheduled run batches because nobody is watching; a run you
started by hand screens instantly because you are.
"""
from __future__ import annotations

import pytest

from job_search.orchestration.coordinator import should_use_batch

THRESHOLD = 2000


class TestAutoRoutesOnOrigin:
    @pytest.mark.parametrize("pending", [1, 40, 146, 1800, 10000])
    def test_scheduled_runs_always_batch(self, pending: int) -> None:
        """Latency is free when nobody is waiting, so take the 50% saving."""
        assert should_use_batch("auto", "scheduled", pending, THRESHOLD) is True

    @pytest.mark.parametrize("pending", [1, 40, 146, 1800])
    def test_manual_runs_screen_instantly(self, pending: int) -> None:
        assert should_use_batch("auto", "manual", pending, THRESHOLD) is False

    @pytest.mark.parametrize("pending", [2000, 5000, 10000])
    def test_manual_runs_batch_a_backlog_too_big_to_sit_through(self, pending: int) -> None:
        assert should_use_batch("auto", "manual", pending, THRESHOLD) is True

    def test_threshold_boundary_is_inclusive(self) -> None:
        assert should_use_batch("auto", "manual", THRESHOLD - 1, THRESHOLD) is False
        assert should_use_batch("auto", "manual", THRESHOLD, THRESHOLD) is True

    def test_threshold_does_not_apply_to_scheduled_runs(self) -> None:
        """A quiet scheduled night still batches; volume is not the signal."""
        assert should_use_batch("auto", "scheduled", 1, THRESHOLD) is True


class TestExplicitModes:
    @pytest.mark.parametrize("origin", ["scheduled", "manual"])
    @pytest.mark.parametrize("pending", [1, 146, 10000])
    def test_instant_never_batches(self, origin: str, pending: int) -> None:
        assert should_use_batch("instant", origin, pending, THRESHOLD) is False

    @pytest.mark.parametrize("origin", ["scheduled", "manual"])
    @pytest.mark.parametrize("pending", [1, 146, 10000])
    def test_batch_always_batches(self, origin: str, pending: int) -> None:
        assert should_use_batch("batch", origin, pending, THRESHOLD) is True


class TestNothingToDo:
    @pytest.mark.parametrize("mode", ["auto", "instant", "batch"])
    @pytest.mark.parametrize("origin", ["scheduled", "manual"])
    def test_empty_queue_never_submits(self, mode: str, origin: str) -> None:
        """Submitting an empty batch would create a job that can never complete."""
        assert should_use_batch(mode, origin, 0, THRESHOLD) is False

    def test_negative_count_is_treated_as_empty(self) -> None:
        assert should_use_batch("batch", "scheduled", -1, THRESHOLD) is False


class TestUnknownMode:
    def test_unknown_mode_falls_back_to_auto_behaviour(self) -> None:
        """A typo in config must not silently disable screening."""
        assert should_use_batch("typo", "scheduled", 100, THRESHOLD) is True
        assert should_use_batch("typo", "manual", 100, THRESHOLD) is False
