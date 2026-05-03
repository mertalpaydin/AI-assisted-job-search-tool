from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from loguru import logger

from job_search.core.database import DatabaseManager


@dataclass
class PipelineQueues:
    """Holds references to all inter-stage queues (injected by coordinator)."""
    details_pending: object = None
    screening_pending: object = None
    cover_letter_pending: object = None


class ShutdownCoordinator:
    """Thread-safe shutdown flag."""

    def __init__(self) -> None:
        self._shutdown = threading.Event()

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested")
        self._shutdown.set()

    def should_shutdown(self) -> bool:
        return self._shutdown.is_set()

    def wait(self, timeout: float) -> bool:
        """Block until shutdown requested or timeout. Returns True if shutdown."""
        return self._shutdown.wait(timeout=timeout)


class StateManager:
    """
    Manages pipeline state: resume from checkpoint and no-new-jobs detection.
    """

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        self._last_new_job_time: float = time.monotonic()
        self._lock = threading.Lock()

    def record_new_job(self) -> None:
        with self._lock:
            self._last_new_job_time = time.monotonic()

    def minutes_since_last_new_job(self) -> float:
        with self._lock:
            return (time.monotonic() - self._last_new_job_time) / 60.0

    def resume(self, queues: PipelineQueues, cl_mode: str = "auto") -> None:
        """
        Populate queues from the database for jobs that were interrupted
        mid-processing in a previous run.
        """
        import queue as q

        pending_details = self._db.get_jobs_pending_details()
        pending_screening = self._db.get_jobs_pending_screening()
        pending_cover_letters = self._db.get_jobs_pending_cover_letter(mode=cl_mode)

        if queues.details_pending is not None:
            for job_id in pending_details:
                queues.details_pending.put(job_id)

        if queues.screening_pending is not None:
            for job_id in pending_screening:
                queues.screening_pending.put(job_id)

        if queues.cover_letter_pending is not None:
            for job_id in pending_cover_letters:
                queues.cover_letter_pending.put(job_id)

        logger.info(
            "Resumed: {} pending details, {} pending screening, {} pending cover letters",
            len(pending_details),
            len(pending_screening),
            len(pending_cover_letters),
        )

    def log_stats(self) -> None:
        stats = self._db.get_stats()
        logger.info(
            "Stats — total: {total_jobs} | details: {with_details} | "
            "screened: {screened} | selected: {selected} | cover letters: {cover_letters_generated}",
            **stats,
        )
