"""Tests for job_search.core.state — ShutdownCoordinator and StateManager."""
from __future__ import annotations

import queue
import time

import pytest

from job_search.core.database import DatabaseManager, ScreeningResult
from job_search.core.state import PipelineQueues, ShutdownCoordinator, StateManager


class TestShutdownCoordinator:
    def test_initially_not_shutdown(self) -> None:
        sc = ShutdownCoordinator()
        assert not sc.should_shutdown()

    def test_request_shutdown_sets_flag(self) -> None:
        sc = ShutdownCoordinator()
        sc.request_shutdown()
        assert sc.should_shutdown()

    def test_wait_returns_false_on_timeout(self) -> None:
        sc = ShutdownCoordinator()
        result = sc.wait(timeout=0.05)
        assert result is False

    def test_wait_returns_true_after_shutdown(self) -> None:
        sc = ShutdownCoordinator()
        sc.request_shutdown()
        result = sc.wait(timeout=1.0)
        assert result is True


class TestStateManager:
    def test_minutes_since_last_new_job_starts_near_zero(self, db: DatabaseManager) -> None:
        sm = StateManager(db)
        elapsed = sm.minutes_since_last_new_job()
        assert elapsed < 0.1

    def test_record_new_job_resets_timer(self, db: DatabaseManager) -> None:
        sm = StateManager(db)
        time.sleep(0.05)
        sm.record_new_job()
        elapsed = sm.minutes_since_last_new_job()
        assert elapsed < 0.01

    def test_resume_populates_details_queue(self, db: DatabaseManager) -> None:
        db.insert_job(1001, "kw", "loc")
        db.insert_job(1002, "kw", "loc")

        sm = StateManager(db)
        details_q: queue.Queue = queue.Queue()
        queues = PipelineQueues(details_pending=details_q)
        sm.resume(queues)

        ids = []
        while not details_q.empty():
            ids.append(details_q.get_nowait())
        assert set(ids) == {1001, 1002}

    def test_resume_populates_screening_queue(self, db: DatabaseManager) -> None:
        db.insert_job(2001, "kw", "loc")
        db.update_job_details(2001, {"title": "Dev"})

        sm = StateManager(db)
        screening_q: queue.Queue = queue.Queue()
        queues = PipelineQueues(screening_pending=screening_q)
        sm.resume(queues)

        ids = []
        while not screening_q.empty():
            ids.append(screening_q.get_nowait())
        assert 2001 in ids

    def test_resume_populates_cover_letter_queue(self, db: DatabaseManager) -> None:
        db.insert_job(3001, "kw", "loc")
        db.update_job_details(3001, {"title": "Dev"})
        db.save_screening_result(
            3001,
            ScreeningResult(0.9, "none", True, True, "Good"),
        )

        sm = StateManager(db)
        cl_q: queue.Queue = queue.Queue()
        queues = PipelineQueues(cover_letter_pending=cl_q)
        sm.resume(queues)

        ids = []
        while not cl_q.empty():
            ids.append(cl_q.get_nowait())
        assert 3001 in ids

    def test_resume_with_none_queues_does_not_raise(self, db: DatabaseManager) -> None:
        sm = StateManager(db)
        sm.resume(PipelineQueues())  # all queues are None
