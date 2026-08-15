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
    """Thread-safe shutdown flag, optionally backed by a stop file.

    The web UI cannot reach into a scheduled run's process, so a stop request
    is expressed as a file. Workers poll their queues on a short timeout and
    check this on every pass, so a graceful stop lands within a few seconds
    rather than at the next monitor tick.
    """

    def __init__(self, stop_file: str | None = None) -> None:
        self._shutdown = threading.Event()
        self._stop_file = stop_file

    def request_shutdown(self) -> None:
        logger.info("Shutdown requested")
        self._shutdown.set()

    def _stop_file_present(self) -> bool:
        if not self._stop_file:
            return False
        from job_search.core.runcontrol import stop_requested
        if stop_requested(self._stop_file):
            if not self._shutdown.is_set():
                logger.info("Stop file detected, shutting down")
                self._shutdown.set()
            return True
        return False

    def should_shutdown(self) -> bool:
        return self._shutdown.is_set() or self._stop_file_present()

    def wait(self, timeout: float) -> bool:
        """Block until shutdown requested or timeout. Returns True if shutdown.

        Long waits are broken into short slices so an external stop request is
        noticed promptly instead of after the full timeout.
        """
        if not self._stop_file:
            return self._shutdown.wait(timeout=timeout)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self.should_shutdown()
            if self._shutdown.wait(timeout=min(5.0, remaining)):
                return True
            if self._stop_file_present():
                return True


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
        # Report outstanding work, mirroring the web UI's Pipeline Runner tab.
        # The cumulative totals (total/details/screened) never move during a
        # screen- or cover-letter-only run, so they carried no signal.
        stats = self._db.get_pipeline_stats()
        errors = stats["details_error"] + stats["screened_error"] + stats["cl_error"]
        logger.info(
            "Pending — details: {} | to be screened: {} | prefiltered: {} | "
            "cover letters: {} | errors: {}",
            stats["details_pending"], stats["screen_pending"],
            stats["prefiltered_total"], stats["cl_pending"], errors,
        )
