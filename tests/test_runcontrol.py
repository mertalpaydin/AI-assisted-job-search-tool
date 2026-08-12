"""Tests for cross-process run control: locking, stop requests, schedule pause."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from job_search.core import runcontrol as rc


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, str]:
    return {
        "lock": str(tmp_path / "runner.lock"),
        "stop": str(tmp_path / "runner.stop"),
        "pause": str(tmp_path / "schedule.paused"),
    }


def _past(hours: int = 1) -> str:
    return (datetime.now().astimezone() - timedelta(hours=hours)).isoformat(timespec="seconds")


class TestRunnerLock:
    def test_acquire_then_contend(self, paths) -> None:
        assert rc.acquire_lock(paths["lock"], "manual", "search") is True
        assert rc.acquire_lock(paths["lock"], "scheduled") is False

    def test_lock_records_holder(self, paths) -> None:
        rc.acquire_lock(paths["lock"], "scheduled", "screen")
        info = rc.read_lock(paths["lock"])
        assert info is not None
        assert info.pid == os.getpid()
        assert info.origin == "scheduled"
        assert info.stages == "screen"

    def test_release_frees_it(self, paths) -> None:
        rc.acquire_lock(paths["lock"], "manual")
        rc.release_lock(paths["lock"])
        assert rc.is_locked(paths["lock"]) is None
        assert rc.acquire_lock(paths["lock"], "scheduled") is True

    def test_missing_lock_is_not_locked(self, paths) -> None:
        assert rc.is_locked(paths["lock"]) is None

    def test_dead_pid_is_ignored(self, paths) -> None:
        """A crashed run must not block every later run."""
        Path(paths["lock"]).write_text(
            json.dumps({"pid": 999999, "started_at": _past(), "origin": "scheduled"}),
            encoding="utf-8",
        )
        assert rc.is_locked(paths["lock"]) is None

    def test_stale_lock_is_ignored(self, paths) -> None:
        Path(paths["lock"]).write_text(
            json.dumps({"pid": 1, "started_at": _past(hours=5), "origin": "scheduled"}),
            encoding="utf-8",
        )
        assert rc.is_locked(paths["lock"], stale_after_minutes=30) is None

    def test_corrupt_lock_is_ignored(self, paths) -> None:
        Path(paths["lock"]).write_text("not json", encoding="utf-8")
        assert rc.is_locked(paths["lock"]) is None

    def test_release_does_not_drop_another_process_lock(self, paths) -> None:
        Path(paths["lock"]).write_text(
            json.dumps({"pid": 999999, "started_at": _past(), "origin": "scheduled"}),
            encoding="utf-8",
        )
        rc.release_lock(paths["lock"])
        assert Path(paths["lock"]).exists()


class TestStopRequests:
    def test_request_detect_clear(self, paths) -> None:
        assert not rc.stop_requested(paths["stop"])
        rc.request_stop(paths["stop"], reason="test")
        assert rc.stop_requested(paths["stop"])
        rc.clear_stop(paths["stop"])
        assert not rc.stop_requested(paths["stop"])

    def test_clear_is_idempotent(self, paths) -> None:
        rc.clear_stop(paths["stop"])
        rc.clear_stop(paths["stop"])


class TestSchedulePause:
    @pytest.mark.parametrize("preset,hours", [("12h", 12), ("24h", 24)])
    def test_relative_presets(self, paths, preset: str, hours: int) -> None:
        resume_at = rc.pause_schedule(paths["pause"], preset)
        assert resume_at is not None
        delta = resume_at - datetime.now().astimezone()
        assert timedelta(hours=hours - 1) < delta <= timedelta(hours=hours)

    def test_tomorrow_morning_lands_on_the_configured_hour(self, paths) -> None:
        resume_at = rc.pause_schedule(paths["pause"], "tomorrow_morning", morning_hour=7)
        assert resume_at.hour == 7
        assert resume_at > datetime.now().astimezone()

    def test_indefinite_has_no_resume_time(self, paths) -> None:
        assert rc.pause_schedule(paths["pause"], "indefinite") is None
        assert rc.pause_remaining(paths["pause"]) == "indefinitely"

    def test_active_pause_is_reported(self, paths) -> None:
        rc.pause_schedule(paths["pause"], "12h")
        assert rc.pause_state(paths["pause"]) is not None
        assert rc.pause_remaining(paths["pause"]).endswith("m")

    def test_expired_pause_auto_resumes_and_deletes_the_file(self, paths) -> None:
        """Auto-resume is lazy: the next reader clears it, so a sleeping laptop
        cannot miss the resume moment."""
        Path(paths["pause"]).write_text(
            json.dumps({"paused_at": _past(2), "resume_at": _past(1)}), encoding="utf-8"
        )
        assert rc.pause_state(paths["pause"]) is None
        assert not Path(paths["pause"]).exists()

    def test_no_pause_file_means_active(self, paths) -> None:
        assert rc.pause_state(paths["pause"]) is None
        assert rc.pause_remaining(paths["pause"]) is None

    def test_manual_resume(self, paths) -> None:
        rc.pause_schedule(paths["pause"], "24h")
        rc.resume_schedule(paths["pause"])
        assert rc.pause_state(paths["pause"]) is None

    def test_corrupt_pause_file_is_cleared(self, paths) -> None:
        Path(paths["pause"]).write_text("not json", encoding="utf-8")
        assert rc.pause_state(paths["pause"]) is None

    def test_resolve_is_absolute_at_pause_time(self, paths) -> None:
        """Resolving now, not at read time, means a DST shift cannot move it."""
        first = rc.resolve_resume_at("12h")
        second = rc.resolve_resume_at("12h")
        assert abs((second - first).total_seconds()) < 2
