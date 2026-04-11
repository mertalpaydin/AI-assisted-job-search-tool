from __future__ import annotations

import queue
import threading
import time
from typing import Any

from loguru import logger

from job_search.core.config import Config, load_secrets
from job_search.core.database import DatabaseManager
from job_search.core.state import PipelineQueues, ShutdownCoordinator, StateManager
from job_search.ai.cover_letter import CoverLetterWorker
from job_search.ai.prompt_manager import PromptManager
from job_search.ai.screener import ScreeningWorker
from job_search.scraping.auth import create_session, make_headers
from job_search.scraping.details import DetailsWorker
from job_search.scraping.search import SearchWorker


class JobSearchCoordinator:
    """
    Main orchestrator. Initialises all queues and workers, then monitors
    shutdown conditions in a loop.

    Pipeline:
        SearchWorker(s) → details_queue
        DetailsWorker(s) → screening_queue
        ScreeningWorker  → cover_letter_queue
        CoverLetterWorker(s) (async)
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._secrets = load_secrets()
        self._db = DatabaseManager(config.database.path)
        self._shutdown = ShutdownCoordinator()
        self._state = StateManager(self._db)

        # Queues
        self._details_queue: queue.Queue = queue.Queue()
        self._screening_queue: queue.Queue = queue.Queue()
        self._cover_letter_queue: queue.Queue = queue.Queue()

        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, resume: bool = True) -> None:
        logger.info("=== AI Job Search Tool starting ===")

        if resume:
            queues = PipelineQueues(
                details_pending=self._details_queue,
                screening_pending=self._screening_queue,
                cover_letter_pending=self._cover_letter_queue,
            )
            self._state.resume(queues)

        self._start_workers()
        self._monitor_loop()

    def cleanup(self) -> None:
        if self._shutdown.should_shutdown():
            return  # Idempotent — prevents double-cleanup from SIGTERM + KeyboardInterrupt
        self._shutdown.request_shutdown()
        for t in self._threads:
            t.join(timeout=10)
        self._db.close()
        logger.info("=== Shutdown complete ===")
        self._state.log_stats()

    # ------------------------------------------------------------------
    # Worker startup
    # ------------------------------------------------------------------

    def _start_web_ui(self) -> None:
        from job_search.web.app import init_app

        flask_app = init_app(self._db)
        host = self._config.web.host
        port = self._config.web.port

        def _run() -> None:
            try:
                flask_app.run(host=host, port=port, debug=False,
                              use_reloader=False, threaded=True)
            except OSError as exc:
                logger.warning("Web UI failed to start on port {}: {}", port, exc)

        # daemon=True: thread exits automatically when the main process exits.
        # NOT added to self._threads — Werkzeug has no programmatic shutdown hook.
        threading.Thread(target=_run, name="web-ui", daemon=True).start()
        logger.info("Web UI started → http://{}:{}/", host, port)

    def _start_workers(self) -> None:
        cfg = self._config

        # --- Web UI (daemon thread, optional) ---
        if cfg.web.auto_start:
            self._start_web_ui()

        # --- Auth ---
        logger.info("Authenticating with LinkedIn…")
        session = create_session(
            email=self._secrets.linkedin_username,
            password=self._secrets.linkedin_password,
        )
        if session is None:
            raise RuntimeError("LinkedIn authentication failed")

        # --- Prompt manager (shared) ---
        prompt_manager = PromptManager()

        # --- Search workers ---
        for i in range(cfg.concurrency.max_search_workers):
            worker = SearchWorker(
                config=cfg,
                session=session,
                db=self._db,
                state=self._state,
                shutdown=self._shutdown,
                details_queue=self._details_queue,
            )
            self._spawn(f"search-{i}", worker.run)

        # --- Details workers ---
        for i in range(cfg.concurrency.max_details_workers):
            worker = DetailsWorker(
                config=cfg,
                session=session,
                db=self._db,
                shutdown=self._shutdown,
                details_queue=self._details_queue,
                screening_queue=self._screening_queue,
            )
            self._spawn(f"details-{i}", worker.run)

        # --- Screening worker (single — GPU bound) ---
        screener = ScreeningWorker(
            config=cfg,
            db=self._db,
            shutdown=self._shutdown,
            screening_queue=self._screening_queue,
            cover_letter_queue=self._cover_letter_queue,
            prompt_manager=prompt_manager,
        )
        self._spawn("screening", screener.run)

        # --- Cover letter worker (runs its own asyncio loop) ---
        api_keys = self._secrets.gemini_api_keys
        if not api_keys:
            logger.warning("No Gemini API keys configured — cover letter generation disabled")
        else:
            cl_worker = CoverLetterWorker(
                config=cfg,
                db=self._db,
                shutdown=self._shutdown,
                cover_letter_queue=self._cover_letter_queue,
                prompt_manager=prompt_manager,
                api_keys=api_keys,
                export_dir=self._config.export.output_dir,
            )
            self._spawn("cover-letter", cl_worker.run)

        logger.info(
            "Workers started: {} search, {} details, 1 screening, {} cover-letter",
            cfg.concurrency.max_search_workers,
            cfg.concurrency.max_details_workers,
            1 if api_keys else 0,
        )

    def _spawn(self, name: str, target) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # ------------------------------------------------------------------
    # Monitor loop
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        cfg = self._config.execution
        check_interval = cfg.shutdown_conditions.check_interval_seconds
        no_new_jobs_limit = cfg.shutdown_conditions.no_new_jobs_minutes
        max_runtime = cfg.max_runtime_hours * 3600
        start_time = time.monotonic()

        logger.info("Monitor loop running (check every {}s)", check_interval)

        while not self._shutdown.should_shutdown():
            self._shutdown.wait(timeout=check_interval)

            if self._shutdown.should_shutdown():
                break

            elapsed = time.monotonic() - start_time
            no_new_minutes = self._state.minutes_since_last_new_job()

            self._state.log_stats()

            # Shutdown condition 1: max runtime
            if elapsed >= max_runtime:
                logger.info("Max runtime reached ({:.1f}h) — shutting down", elapsed / 3600)
                self._shutdown.request_shutdown()
                break

            # Shutdown condition 2: no new jobs for N minutes AND all queues empty
            queues_empty = (
                self._details_queue.empty()
                and self._screening_queue.empty()
                and self._cover_letter_queue.empty()
            )
            if no_new_minutes >= no_new_jobs_limit and queues_empty:
                logger.info(
                    "No new jobs for {:.1f} min and all queues empty — shutting down",
                    no_new_minutes,
                )
                self._shutdown.request_shutdown()
                break

        logger.info("Monitor loop exiting — waiting for workers to finish…")
        self._drain_queues(timeout=60)

    def _drain_queues(self, timeout: float) -> None:
        """Give workers up to `timeout` seconds to finish current items."""
        deadline = time.monotonic() + timeout
        for q in (self._details_queue, self._screening_queue, self._cover_letter_queue):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    q.join()  # blocks until all task_done() called
                except Exception:
                    pass
