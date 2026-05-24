"""Tests for DetailsWorker 404 handling — job should be deleted, not errored."""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_search.core.database import DatabaseManager
from job_search.core.state import ShutdownCoordinator
from job_search.scraping.details import DetailsWorker


def _make_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = str(json_body)
    resp.json.return_value = json_body or {}
    return resp


def _run_worker_until_queue_empty(
    w: DetailsWorker,
    details_q: queue.Queue,
    shutdown: ShutdownCoordinator,
) -> None:
    """Start worker in a thread, wait for queue to drain, then shut it down."""
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    details_q.join()          # blocks until all task_done() calls complete
    shutdown.request_shutdown()
    t.join(timeout=10)


@pytest.fixture()
def worker_parts(tmp_path: Path, db: DatabaseManager):
    session = MagicMock()
    shutdown = ShutdownCoordinator()
    details_q: queue.Queue = queue.Queue()
    screening_q: queue.Queue = queue.Queue()

    config = MagicMock()
    config.search.rate_limits.delay_between_requests = 0

    w = DetailsWorker(
        config=config,
        session=session,
        db=db,
        shutdown=shutdown,
        details_queue=details_q,
        screening_queue=screening_q,
    )
    return w, session, shutdown, details_q, screening_q


class TestDetails404Handling:
    def test_404_deletes_job_from_db(self, worker_parts, db: DatabaseManager) -> None:
        w, session, shutdown, details_q, screening_q = worker_parts

        db.insert_job(5500, "kw", "loc")
        assert db.job_exists(5500)

        session.get.return_value = _make_response(404, {"data": {"status": 404}, "included": []})
        details_q.put(5500)

        _run_worker_until_queue_empty(w, details_q, shutdown)

        assert not db.job_exists(5500), "Job should have been deleted on 404"
        assert screening_q.empty(), "404 jobs should not reach the screening queue"

    def test_404_does_not_increment_error_count(self, worker_parts, db: DatabaseManager) -> None:
        w, session, shutdown, details_q, screening_q = worker_parts

        session.get.return_value = _make_response(404, {"data": {"status": 404}, "included": []})

        for job_id in range(6000, 6010):
            db.insert_job(job_id, "kw", "loc")
            details_q.put(job_id)

        _run_worker_until_queue_empty(w, details_q, shutdown)

        for job_id in range(6000, 6010):
            assert not db.job_exists(job_id), f"Job {job_id} should have been deleted"

    def test_non_404_error_marks_job_as_error(self, worker_parts, db: DatabaseManager) -> None:
        w, session, shutdown, details_q, screening_q = worker_parts

        db.insert_job(7700, "kw", "loc")
        session.get.return_value = _make_response(500, {})
        details_q.put(7700)

        _run_worker_until_queue_empty(w, details_q, shutdown)

        row = db.get_job_details(7700)
        assert row is not None
        assert row.scraped == -1, "Non-404 errors should mark job as error (scraped=-1)"
