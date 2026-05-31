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
from job_search.ai.screener import GeminiScreeningWorker, ScreeningWorker
from job_search.utils.api_rotation import GeminiAPIRotator
from job_search.scraping.auth import create_session, make_headers
from job_search.scraping.details import DetailsWorker
from job_search.scraping.search import SearchWorker


ALL_STAGES = ("search", "details", "screen", "cover-letter")


class JobSearchCoordinator:
    """
    Main orchestrator. Initialises all queues and workers, then monitors
    shutdown conditions in a loop.

    Pipeline:
        SearchWorker(s) → details_queue
        DetailsWorker(s) → screening_queue
        ScreeningWorker(s) → cover_letter_queue
        CoverLetterWorker(s) (async)

    Use `stages` to run a subset of the pipeline, e.g. stages={"screen", "cover-letter"}
    to process jobs already in the database without re-scraping.
    """

    def __init__(self, config: Config, stages: set[str] | None = None) -> None:
        self._config = config
        self._stages = set(stages) if stages else set(ALL_STAGES)
        self._secrets = load_secrets()
        self._db = DatabaseManager(config.database.path)
        self._shutdown = ShutdownCoordinator()
        self._state = StateManager(self._db)

        # Queues
        self._details_queue: queue.Queue = queue.Queue()
        self._screening_queue: queue.Queue = queue.Queue()
        self._cover_letter_queue: queue.Queue = queue.Queue()

        self._threads: list[threading.Thread] = []
        self._cleaned_up = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, resume: bool = True) -> None:
        active = sorted(self._stages)
        logger.info("=== AI Job Search Tool starting (stages: {}) ===", ", ".join(active))

        if resume:
            queues = PipelineQueues(
                details_pending=self._details_queue if "details" in self._stages else None,
                screening_pending=self._screening_queue if "screen" in self._stages else None,
                cover_letter_pending=self._cover_letter_queue if "cover-letter" in self._stages else None,
            )
            self._state.resume(queues, cl_mode=self._config.cover_letter.mode)

        self._start_workers()
        self._monitor_loop()

    def cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
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

        flask_app = init_app(self._db, config=self._config)
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
        stages = self._stages

        # --- Web UI (daemon thread, optional) ---
        if cfg.web.auto_start:
            self._start_web_ui()

        # --- Prompt manager (shared by screening and cover letter workers) ---
        prompt_manager = PromptManager()

        # --- LinkedIn auth (only needed for scraping stages) ---
        session = None
        if stages & {"search", "details"}:
            _MAX_LOGIN_ATTEMPTS = 5
            _LOGIN_RETRY_DELAY  = 15  # seconds between attempts
            for attempt in range(1, _MAX_LOGIN_ATTEMPTS + 1):
                logger.info(
                    "Authenticating with LinkedIn (attempt {}/{})…",
                    attempt, _MAX_LOGIN_ATTEMPTS,
                )
                session = create_session(
                    email=self._secrets.linkedin_username,
                    password=self._secrets.linkedin_password,
                    attempt=attempt,
                )
                if session is not None:
                    break
                if attempt < _MAX_LOGIN_ATTEMPTS:
                    logger.warning(
                        "Login attempt {} failed — retrying in {}s…",
                        attempt, _LOGIN_RETRY_DELAY,
                    )
                    time.sleep(_LOGIN_RETRY_DELAY)
            if session is None:
                raise RuntimeError(
                    f"LinkedIn authentication failed after {_MAX_LOGIN_ATTEMPTS} attempts. "
                    "Check debug/ screenshots and config/.env credentials."
                )

        # --- Search workers ---
        if "search" in stages:
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
        if "details" in stages:
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

        # --- Screening workers (Gemini API or local GGUF) ---
        n_screening = 0
        screening_backend = cfg.screening.backend
        api_keys = self._secrets.gemini_api_keys

        if "screen" in stages and screening_backend == "gemini":
            if not api_keys:
                raise RuntimeError(
                    "screening.backend is 'gemini' but no Gemini API keys are configured. "
                    "Set GEMINI_API_KEY_1 (and optionally _2/_3) in config/.env"
                )
            screening_rotator = GeminiAPIRotator(
                api_keys,
                requests_per_minute=cfg.screening.gemini.requests_per_minute,
            )
            n_screening = cfg.concurrency.max_screening_workers
            for i in range(n_screening):
                worker = GeminiScreeningWorker(
                    config=cfg,
                    db=self._db,
                    shutdown=self._shutdown,
                    screening_queue=self._screening_queue,
                    cover_letter_queue=self._cover_letter_queue,
                    prompt_manager=prompt_manager,
                    rotator=screening_rotator,
                    worker_id=i,
                )
                self._spawn(f"screening-gemini-{i}", worker.run)
        elif "screen" in stages:
            n_screening = 1
            screener = ScreeningWorker(
                config=cfg,
                db=self._db,
                shutdown=self._shutdown,
                screening_queue=self._screening_queue,
                cover_letter_queue=self._cover_letter_queue,
                prompt_manager=prompt_manager,
            )
            self._spawn("screening-local", screener.run)

        # --- Cover letter worker (runs its own asyncio loop) ---
        if "cover-letter" not in stages:
            pass
        elif not api_keys:
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
            "Workers started: {} search, {} details, {} screening ({}), {} cover-letter",
            cfg.concurrency.max_search_workers,
            cfg.concurrency.max_details_workers,
            n_screening,
            screening_backend,
            len(api_keys) if api_keys else 0,
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
        max_runtime = cfg.max_runtime_hours * 3600
        start_time = time.monotonic()
        retry_interval = cfg.retry_errors_interval_minutes * 60
        last_retry = time.monotonic() if retry_interval > 0 else None

        # When search is not active there are no new jobs being discovered,
        # so use a short idle timeout to shut down promptly after queues drain.
        if "search" in self._stages:
            idle_limit_minutes = cfg.shutdown_conditions.no_new_jobs_minutes
        else:
            idle_limit_minutes = 15

        # Only watch queues that belong to active stages
        watched_queues: list[queue.Queue] = []
        if "details" in self._stages or "search" in self._stages:
            watched_queues.append(self._details_queue)
        if "screen" in self._stages:
            watched_queues.append(self._screening_queue)
        if "cover-letter" in self._stages:
            watched_queues.append(self._cover_letter_queue)

        if retry_interval > 0:
            logger.info(
                "Monitor loop running (check every {}s, idle limit {}min, error retry every {}min)",
                check_interval, idle_limit_minutes, cfg.retry_errors_interval_minutes,
            )
        else:
            logger.info("Monitor loop running (check every {}s, idle limit {}min)",
                        check_interval, idle_limit_minutes)

        while not self._shutdown.should_shutdown():
            self._shutdown.wait(timeout=check_interval)

            if self._shutdown.should_shutdown():
                break

            elapsed = time.monotonic() - start_time
            no_new_minutes = self._state.minutes_since_last_new_job()

            self._state.log_stats()

            # Auto-retry errored jobs
            if last_retry is not None and (time.monotonic() - last_retry) >= retry_interval:
                self._retry_errors()
                last_retry = time.monotonic()

            # Shutdown condition 1: max runtime
            if elapsed >= max_runtime:
                logger.info("Max runtime reached ({:.1f}h) — shutting down", elapsed / 3600)
                self._shutdown.request_shutdown()
                break

            # Shutdown condition 2: no new jobs for N minutes AND active queues empty
            queues_empty = all(q.empty() for q in watched_queues)
            if no_new_minutes >= idle_limit_minutes and queues_empty:
                logger.info(
                    "No new jobs for {:.1f} min and active queues empty — shutting down",
                    no_new_minutes,
                )
                self._shutdown.request_shutdown()
                break

        logger.info("Monitor loop exiting — waiting for workers to finish…")
        self._drain_queues(timeout=60)

    def _retry_errors(self) -> None:
        """Reset errored jobs and push them back onto the live queues."""
        stages = self._stages

        if "details" in stages:
            job_ids = self._db.reset_detail_errors()
            for jid in job_ids:
                self._details_queue.put(jid)
            if job_ids:
                logger.info("Auto-retry: requeued {} detail-error job(s)", len(job_ids))

        if "screen" in stages:
            job_ids = self._db.reset_screening_errors()
            for jid in job_ids:
                self._screening_queue.put(jid)
            if job_ids:
                logger.info("Auto-retry: requeued {} screening-error job(s)", len(job_ids))

        if "cover-letter" in stages:
            cleared = self._db.purge_cover_letter_errors()
            if cleared:
                # Re-queue only jobs still eligible under current CL mode
                eligible = set(self._db.get_jobs_pending_cover_letter(
                    mode=self._config.cover_letter.mode
                ))
                to_retry = [jid for jid in cleared if jid in eligible]
                for jid in to_retry:
                    self._cover_letter_queue.put(jid)
                logger.info(
                    "Auto-retry: requeued {}/{} cover-letter-error job(s)",
                    len(to_retry), len(cleared),
                )

    def _drain_queues(self, timeout: float) -> None:
        """Give workers up to `timeout` seconds to finish in-flight items.

        queue.Queue.join() has no built-in timeout, so we run each join in a
        daemon thread and wait on it with a deadline. Items still in the queue
        when workers stop will never call task_done(), so a plain join() would
        block forever — this prevents that.
        """
        deadline = time.monotonic() + timeout
        for q in (self._details_queue, self._screening_queue, self._cover_letter_queue):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t = threading.Thread(target=q.join, daemon=True)
            t.start()
            t.join(timeout=remaining)
