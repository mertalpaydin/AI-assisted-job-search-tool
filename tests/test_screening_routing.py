"""Tests for screening mode routing.

Two rules, and they interact.

The first: "auto" decides on who is waiting, not on how many jobs are queued.
A scheduled run batches because nobody is watching; a run you started by hand
screens instantly because you are. The threshold is the single exception, for
a manual run against a backlog too large to sit through.

The second: the count describes the backlog, it does not gate the decision.
An empty queue does not make a run instant. This one cost real money before it
was fixed: a scheduled run that happened to start with an empty queue spawned
instant screening workers and paid full price for every job it went on to
scrape. "Is there anything to send right now" is a separate question, asked at
each submission rather than once at startup.
"""
from __future__ import annotations

import pytest

from job_search.orchestration.coordinator import batch_routed

THRESHOLD = 2000


class TestAutoRoutesOnOrigin:
    @pytest.mark.parametrize("pending", [1, 40, 146, 1800, 10000])
    def test_scheduled_runs_always_batch(self, pending: int) -> None:
        """Latency is free when nobody is waiting, so take the 50% saving."""
        assert batch_routed("auto", "scheduled", pending, THRESHOLD) is True

    @pytest.mark.parametrize("pending", [1, 40, 146, 1800])
    def test_manual_runs_screen_instantly(self, pending: int) -> None:
        assert batch_routed("auto", "manual", pending, THRESHOLD) is False

    @pytest.mark.parametrize("pending", [2000, 5000, 10000])
    def test_manual_runs_batch_a_backlog_too_big_to_sit_through(self, pending: int) -> None:
        assert batch_routed("auto", "manual", pending, THRESHOLD) is True

    def test_threshold_boundary_is_inclusive(self) -> None:
        assert batch_routed("auto", "manual", THRESHOLD - 1, THRESHOLD) is False
        assert batch_routed("auto", "manual", THRESHOLD, THRESHOLD) is True

    def test_threshold_does_not_apply_to_scheduled_runs(self) -> None:
        """A scheduled run batches at any size, so the threshold never bites."""
        assert batch_routed("auto", "scheduled", 1, THRESHOLD) is True


class TestExplicitModesOverrideOrigin:
    @pytest.mark.parametrize("origin", ["manual", "scheduled"])
    @pytest.mark.parametrize("pending", [1, 10000])
    def test_instant_never_batches(self, origin: str, pending: int) -> None:
        assert batch_routed("instant", origin, pending, THRESHOLD) is False

    @pytest.mark.parametrize("origin", ["manual", "scheduled"])
    @pytest.mark.parametrize("pending", [1, 10000])
    def test_batch_always_batches(self, origin: str, pending: int) -> None:
        assert batch_routed("batch", origin, pending, THRESHOLD) is True

    def test_unknown_mode_falls_back_to_auto_behaviour(self) -> None:
        """A typo in config should not silently disable batching."""
        assert batch_routed("typo", "scheduled", 100, THRESHOLD) is True
        assert batch_routed("typo", "manual", 100, THRESHOLD) is False


class TestVolumeDoesNotGateRouting:
    """The regression that made scheduled runs pay full price.

    Routing decides whether screening workers are spawned at all, and that has
    to be settled before the first job is scraped. Reading an empty queue as
    "nothing to batch" collapsed a whole scheduled run into instant screening.
    """

    def test_scheduled_run_starting_empty_is_still_a_batch_run(self) -> None:
        assert batch_routed("auto", "scheduled", 0, THRESHOLD) is True

    def test_batch_mode_starting_empty_is_still_a_batch_run(self) -> None:
        assert batch_routed("batch", "manual", 0, THRESHOLD) is True

    def test_instant_mode_starting_empty_is_still_an_instant_run(self) -> None:
        assert batch_routed("instant", "scheduled", 0, THRESHOLD) is False

    def test_manual_run_starting_empty_is_an_instant_run(self) -> None:
        """Not because the queue is empty, but because 0 is under threshold.

        A manual run that later crosses the threshold deliberately stays
        instant: its screening workers are already holding those jobs, and
        batching them too would pay for both.
        """
        assert batch_routed("auto", "manual", 0, THRESHOLD) is False

    def test_a_nonsense_negative_count_reads_as_a_small_backlog(self) -> None:
        """Never raises, and never flips a manual run into batch."""
        assert batch_routed("auto", "manual", -1, THRESHOLD) is False
        assert batch_routed("auto", "scheduled", -1, THRESHOLD) is True
        assert batch_routed("batch", "manual", -1, THRESHOLD) is True
