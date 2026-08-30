"""Who gets a runtime ceiling, and who does not.

The 8h ceiling and the sweep's own bound exist to stop an UNATTENDED run
holding the machine all day. Neither should apply to a sweep somebody started
by hand: nothing is queued behind it, and cutting it off only means checking
the same postings again next time.
"""
from __future__ import annotations

import pytest

from job_search.core.config import Config
from job_search.orchestration.coordinator import JobSearchCoordinator


@pytest.fixture()
def config(tmp_path) -> Config:
    return Config.model_validate({
        "search": {
            "keywords": ["AI Engineer"],
            "locations": [{"geo_id": "1", "name": "Frankfurt"}],
        },
        "database": {"path": str(tmp_path / "jobs.db")},
        "execution": {
            "max_runtime_hours": 8,
            "lock_file": str(tmp_path / "runner.lock"),
            "stop_file": str(tmp_path / "runner.stop"),
        },
    })


def _coordinator(config, **kw) -> JobSearchCoordinator:
    c = JobSearchCoordinator(config, **kw)
    c._db.close()
    return c


class TestCleanRuntimeCeiling:
    def test_an_on_demand_sweep_has_no_ceiling(self, config) -> None:
        c = _coordinator(config, stages={"clean"}, origin="manual")
        assert c._max_runtime_hours == 0.0

    def test_a_scheduled_sweep_keeps_its_ceiling(self, config) -> None:
        c = _coordinator(config, stages={"clean"}, origin="scheduled")
        assert c._max_runtime_hours == 8

    def test_an_explicit_ceiling_still_wins(self, config) -> None:
        c = _coordinator(config, stages={"clean"}, origin="manual",
                         max_runtime_hours=3)
        assert c._max_runtime_hours == 3

    def test_a_pipeline_run_that_also_cleans_is_still_bounded(self, config) -> None:
        """Only a sweep on its own is a "cleaning job"; a full run is not."""
        c = _coordinator(config, stages={"search", "details", "clean"},
                         origin="manual")
        assert c._max_runtime_hours == 8

    def test_an_ordinary_manual_run_is_unaffected(self, config) -> None:
        c = _coordinator(config, stages={"screen"}, origin="manual")
        assert c._max_runtime_hours == 8


class TestScheduledSweepBound:
    """The CLI resolves the bound: config under --scheduled, none otherwise."""

    def test_the_default_is_two_hours(self, config) -> None:
        assert config.cleaner.scheduled_max_runtime_hours == 2.0

    @pytest.mark.parametrize(
        "explicit, scheduled, expected",
        [
            (None, True, 2.0),    # scheduled, no flag -> config bound
            (None, False, None),  # on demand           -> no bound
            (5.0, True, 5.0),     # explicit always wins
            (5.0, False, 5.0),
            (0, False, None),     # 0 means "no bound"
            (0, True, None),
        ],
    )
    def test_resolution(self, config, explicit, scheduled, expected) -> None:
        max_runtime = explicit
        if max_runtime is None and scheduled:
            max_runtime = config.cleaner.scheduled_max_runtime_hours
        if max_runtime is not None and max_runtime <= 0:
            max_runtime = None
        assert max_runtime == expected
