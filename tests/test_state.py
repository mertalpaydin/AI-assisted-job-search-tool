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
            ScreeningResult(0.9, "none", True, "Good"),
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

    def test_log_stats_reports_actual_auto_and_separates_cumulative(self, db: DatabaseManager) -> None:
        from unittest.mock import patch
        from job_search.core.config import AutoScreenConfig

        # Job 1: deferred (german)
        db.insert_job(4001, "kw", "loc")
        db.update_job_details(4001, {
            "title": "German Job",
            "scraped": 1,
            "detected_language": "de",
            "german_stopword_ratio": 0.85,
            "company_staff_count": 500,
        })

        # Job 2: deferred (small company)
        db.insert_job(4002, "kw", "loc")
        db.update_job_details(4002, {
            "title": "Small Co Job",
            "scraped": 1,
            "detected_language": "en",
            "german_stopword_ratio": 0.02,
            "company_staff_count": 25,
        })

        # Job 3: prefiltered
        db.insert_job(4003, "kw", "loc", prefilter_reason="excluded_keyword")

        auto_cfg = AutoScreenConfig(enabled=True, min_company_size="mid", exclude_fully_german=True)
        sm = StateManager(db, auto_screen_cfg=auto_cfg)

        with patch("job_search.core.state.logger.info") as mock_logger:
            sm.log_stats()
            mock_logger.assert_called_once()
            log_msg = mock_logger.call_args[0][0].format(*mock_logger.call_args[0][1:])
            # Actual auto to be screened must be 0, not 2!
            assert "to be screened: 0" in log_msg
            assert "Cumulative — deferred: 2 | prefiltered: 1" in log_msg
            assert "Pending — details: 0 | to be screened: 0 | cover letters: 0 | errors: 0" in log_msg

