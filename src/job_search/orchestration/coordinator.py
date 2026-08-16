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
from job_search.core import runcontrol
from job_search.scraping.auth import get_session, make_headers
from job_search.scraping.details import DetailsWorker
from job_search.scraping.search import SearchWorker


ALL_STAGES = ("search", "details", "screen", "cover-letter", "clean")

# Stages that never touch LinkedIn, so they still run without a valid session.
OFFLINE_STAGES = ("screen", "cover-letter", "collect-batches")


def batch_routed(mode: str, origin: str, pending_count: int,
                 threshold: int) -> bool:
    """Decide whether this run screens via batch rather than instantly.

    "auto" routes on who is waiting rather than on volume, because volume says
    nothing about whether anyone is watching. A scheduled run has nobody in
    front of it, so batch latency costs nothing and the 50% saving is free. A
    run started by hand should return answers now, at any size. The threshold
    is a narrow override for the one case where that breaks down: a manual run
    against a backlog too large to sit through synchronously.

    The count is read as "how big is this backlog", never as "is there work
    right now". An empty queue does not make a run instant: a scheduled run
    usually starts with nothing pending and goes on to scrape thousands of
    jobs, and it should batch every one of them. Whether there is anything to
    send is a separate question, asked at each submission.

    A run is either a batch run or an instant run for its whole life. Switching
    mid-run would mean submitting jobs that live screening workers are already
    holding, and paying for both.
    """
    if mode == "batch":
        return True
    if mode == "instant":
        return False
    # auto
    if origin == "scheduled":
        return True
    return pending_count >= threshold


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

    def __init__(
        self,
        config: Config,
        stages: set[str] | None = None,
        interactive: bool = True,
        origin: str = "manual",
        max_runtime_hours: float | None = None,
    ) -> None:
        self._config = config
        self._stages = set(stages) if stages else set(ALL_STAGES)
        self._secrets = load_secrets()
        self._db = DatabaseManager(config.database.path)
        self._shutdown = ShutdownCoordinator(stop_file=config.execution.stop_file)
        self._state = StateManager(self._db)
        self._interactive = interactive
        self._origin = origin
        self._max_runtime_hours = (
            max_runtime_hours if max_runtime_hours is not None
            else config.execution.max_runtime_hours
        )
        self._lock_held = False
        # Set when LinkedIn stages were skipped because no valid session exists.
        self.linkedin_session_invalid = False
        # True once this run has committed to batch screening. Screening
        # workers are never spawned in that case, so the monitor loop is the
        # only thing that moves screening work forward.
        self._batch_routed = False
        self._last_batch_poll = 0.0

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
        exec_cfg = self._config.execution

        # A scheduled run must never fight a manual one, and vice versa.
        if not runcontrol.acquire_lock(
            exec_cfg.lock_file, origin=self._origin, stages=",".join(active),
            stale_after_minutes=exec_cfg.lock_stale_after_minutes,
        ):
            holder = runcontrol.read_lock(exec_cfg.lock_file)
            logger.warning(
                "Another run is already in progress (pid {}, {} run, started {}). Exiting.",
                getattr(holder, "pid", "?"), getattr(holder, "origin", "?"),
                getattr(holder, "started_at", "?"),
            )
            return
        self._lock_held = True

        # A stop request left over from a previous run would kill this one instantly.
        runcontrol.clear_stop(exec_cfg.stop_file)

        logger.info("=== AI Job Search Tool starting (stages: {}) ===", ", ".join(active))

        # Collect finished batches before anything else. This is what makes
        # pressing Start enough: yesterday's results land, their jobs stop being
        # in flight, and the run proceeds with what is genuinely outstanding.
        if "screen" in self._stages:
            self._collect_batches()

        if resume:
            self._cleanup_errors_on_start()
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
        if self._lock_held:
            runcontrol.release_lock(self._config.execution.lock_file)
            runcontrol.clear_stop(self._config.execution.stop_file)
        logger.info("=== Shutdown complete ===")
        self._state.log_stats(cl_mode=self._config.cover_letter.mode)

    def _collect_batches(self) -> None:
        """Write back any finished screening batches. Never fatal."""
        try:
            from job_search.ai.batch_screener import BatchScreener

            api_keys = self._secrets.gemini_api_keys
            if not api_keys:
                return
            open_batches = self._db.get_open_batch_jobs()
            if not open_batches:
                return

            logger.info("Checking {} open screening batch(es) before starting",
                        len(open_batches))
            screener = BatchScreener(self._config, self._db, api_key=api_keys[0])
            summary = screener.collect_all(
                stale_after_hours=self._config.screening.batch_stale_after_hours
            )
            if summary["collected"]:
                logger.info("Collected {} screening result(s) from batches",
                            summary["collected"])
            if summary["still_running"]:
                in_flight = sum(
                    b["request_count"] for b in self._db.get_open_batch_jobs()
                )
                logger.info(
                    "{} job(s) are still awaiting batch results and will be "
                    "skipped this run. Abandon the batch if you want them now.",
                    in_flight,
                )
        except Exception as exc:
            logger.warning("Batch collection skipped: {}", exc)

    def _cleanup_errors_on_start(self) -> None:
        """Reset all stage errors before resume so they are re-queued naturally.

        Called once at startup (before ``StateManager.resume``). Error rows from
        a previous session are cleared so the jobs become "pending" again and are
        picked up by the resume logic alongside any other unfinished work.
        """
        stages = self._stages
        total = 0

        if "details" in stages:
            job_ids = self._db.reset_detail_errors()
            if job_ids:
                logger.info(
                    "Startup cleanup: {} detail-error job(s) reset for retry", len(job_ids)
                )
                total += len(job_ids)

        if "screen" in stages:
            job_ids = self._db.reset_screening_errors()
            if job_ids:
                logger.info(
                    "Startup cleanup: {} screening-error job(s) reset for retry", len(job_ids)
                )
                total += len(job_ids)

        if "cover-letter" in stages:
            job_ids = self._db.purge_cover_letter_errors()
            if job_ids:
                logger.info(
                    "Startup cleanup: {} cover-letter-error job(s) reset for retry", len(job_ids)
                )
                total += len(job_ids)

        if total == 0:
            logger.debug("Startup cleanup: no error jobs found")

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

        # --- LinkedIn auth (needed for scraping & clean stages) ---
        session = None
        linkedin_stages = stages & {"search", "details", "clean"}
        if linkedin_stages:
            auth_cfg = self._config.auth
            session = get_session(
                email=self._secrets.linkedin_username,
                password=self._secrets.linkedin_password,
                session_file=auth_cfg.session_file,
                interactive=self._interactive,
                interactive_timeout=auth_cfg.interactive_timeout,
                validate=auth_cfg.validate_on_start,
            )
            if session is None:
                # Drop the LinkedIn stages but keep everything that only needs
                # Gemini, so a dead session costs scraping and nothing else.
                self.linkedin_session_invalid = True
                self._stages -= linkedin_stages
                stages = self._stages
                remaining = sorted(stages)
                if not remaining:
                    logger.error(
                        "No valid LinkedIn session and no non-LinkedIn stages to run. "
                        "Run 'job-search login' to sign in."
                    )
                    return
                logger.warning(
                    "No valid LinkedIn session. Skipping {} and continuing with {}.",
                    ", ".join(sorted(linkedin_stages)), ", ".join(remaining),
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

        # A batch run never spawns screening workers. The monitor loop submits
        # and collects for it instead, so work that arrives later in the run is
        # still screened without a restart.
        screening_mode = getattr(cfg.screening, "mode", "instant")
        if "screen" in stages and screening_backend == "gemini" and api_keys:
            pending = self._db.get_jobs_pending_screening()
            self._batch_routed = batch_routed(
                screening_mode, self._origin, len(pending), cfg.screening.batch_threshold
            )
            if self._batch_routed:
                logger.info(
                    "Screening via BATCH this run (mode={}, {} run, {} pending now)",
                    screening_mode, self._origin, len(pending),
                )
                # An empty queue is not a failure: a scheduled run often starts
                # with nothing pending and fills up as it scrapes.
                if pending and not self._submit_batch(pending, api_keys[0]):
                    logger.warning(
                        "First batch was rejected, falling back to instant "
                        "screening for this run"
                    )
                    self._batch_routed = False
                else:
                    self._last_batch_poll = time.monotonic()
                    stages = self._stages = self._stages - {"screen"}
            elif pending:
                logger.info(
                    "Screening {} job(s) INSTANTLY (mode={}, {} run)",
                    len(pending), screening_mode, self._origin,
                )

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

        # --- Cleaner worker ---
        if "clean" in stages:
            def run_cleaner():
                from job_search.cleaner.cleaner import JobCleaner
                cleaner = JobCleaner(self._db, session=session)
                cleaner.clean_pending_jobs()
            self._spawn("cleaner", run_cleaner)

        logger.info(
            "Workers started: {} search, {} details, {} screening ({}), {} cover-letter",
            cfg.concurrency.max_search_workers,
            cfg.concurrency.max_details_workers,
            n_screening,
            screening_backend,
            len(api_keys) if api_keys else 0,
        )

    def _submit_batch(self, pending: list[int], api_key: str) -> bool:
        """Send pending screening work to the Batch API.

        Returns True when the batch was accepted, so the caller knows it is safe
        to skip the synchronous screening workers. On any failure this returns
        False and the run screens instantly instead, which matters because a
        silently skipped screening stage looks identical to "nothing to do".
        """
        try:
            from job_search.ai.batch_screener import BatchScreener

            screener = BatchScreener(self._config, self._db, api_key=api_key)
            batch_id = screener.submit(pending)
        except Exception as exc:
            logger.error("Batch submission failed, screening instantly instead: {}", exc)
            return False

        if batch_id is None:
            logger.info("Batch submission produced no work, screening instantly instead")
            return False

        logger.info(
            "Screening submitted as batch {} ({} jobs). Results arrive within "
            "24h and are collected by the next run or the collect task.",
            batch_id, len(pending),
        )
        return True

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
        max_runtime = self._max_runtime_hours * 3600
        start_time = time.monotonic()
        retry_interval = cfg.retry_errors_interval_minutes * 60
        last_retry = time.monotonic() if retry_interval > 0 else None
        batch_poll_interval = max(
            60.0, getattr(self._config.screening, "batch_poll_minutes", 10.0) * 60
        )
        if not self._last_batch_poll:
            self._last_batch_poll = time.monotonic()

        # When search is not active there are no new jobs being discovered,
        # so use a short idle timeout to shut down promptly after queues drain.
        if "search" in self._stages:
            idle_limit_minutes = cfg.shutdown_conditions.no_new_jobs_minutes
        else:
            # A drain-only run should exit as soon as the queues are empty,
            # not sit holding the lock.
            idle_limit_minutes = cfg.idle_drain_minutes

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

            self._state.log_stats(cl_mode=self._config.cover_letter.mode)

            # Auto-retry errored jobs
            if last_retry is not None and (time.monotonic() - last_retry) >= retry_interval:
                self._retry_errors()
                last_retry = time.monotonic()

            # Batch upkeep: collect anything that finished, and submit another
            # batch once enough new work has accumulated. Both were previously
            # startup-only, which is why a long run could neither pick up its
            # own results nor screen anything it scraped after the first batch.
            if (time.monotonic() - self._last_batch_poll) >= batch_poll_interval:
                self._last_batch_poll = time.monotonic()
                self._batch_upkeep()

            # Shutdown condition 1: max runtime
            if elapsed >= max_runtime:
                logger.info("Max runtime reached ({:.1f}h) — shutting down", elapsed / 3600)
                self._shutdown.request_shutdown()
                break

            # Shutdown condition 2: no new jobs for N minutes AND active queues empty AND no non-queue workers running
            queues_empty = all(q.empty() for q in watched_queues)
            # Check if any non-queue worker thread (like cleaner) is still running
            cleaner_running = any(
                t.is_alive() and t.name == "cleaner" for t in self._threads
            )

            if queues_empty and not cleaner_running:
                # Before declaring idle shutdown, check if there are errored jobs to retry
                if retry_interval > 0:
                    requeued = self._retry_errors()
                    if requeued > 0:
                        logger.info(
                            "Queues were empty, but auto-retry re-queued {} errored job(s) for processing.",
                            requeued,
                        )
                        last_retry = time.monotonic()
                        continue

                if no_new_minutes >= idle_limit_minutes:
                    logger.info(
                        "No new jobs for {:.1f} min, active queues empty, and no active workers — shutting down",
                        no_new_minutes,
                    )
                    self._shutdown.request_shutdown()
                    break

        logger.info("Monitor loop exiting — waiting for workers to finish…")
        self._drain_queues(timeout=60)

    def _batch_upkeep(self) -> None:
        """Collect finished batches, and submit another if enough work waits.

        Runs on the monitor loop's tick. Deliberately never fatal: a screening
        batch is an optimisation, and a provider hiccup should not take down a
        run that is otherwise scraping happily.
        """
        try:
            if self._db.get_open_batch_jobs():
                # Worth doing even on an instant run: an older batch may still
                # be open from a previous one, and its jobs are excluded from
                # the pending queue until its results land.
                self._collect_batches()

            if not self._batch_routed:
                return

            api_keys = self._secrets.gemini_api_keys
            if not api_keys:
                return

            pending = self._db.get_jobs_pending_screening()
            threshold = self._config.screening.batch_threshold
            if len(pending) < threshold:
                return

            logger.info(
                "{} job(s) have accumulated since the last batch, submitting another",
                len(pending),
            )
            self._submit_batch(pending, api_keys[0])
        except Exception as exc:
            logger.warning("Batch upkeep skipped this tick: {}", exc)

    def _retry_errors(self) -> int:
        """Reset errored jobs and push them back onto the live queues. Returns total requeued count."""
        stages = self._stages
        requeued_total = 0

        if "details" in stages:
            job_ids = self._db.reset_detail_errors()
            for jid in job_ids:
                self._details_queue.put(jid)
            if job_ids:
                logger.info("Auto-retry: requeued {} detail-error job(s)", len(job_ids))
                requeued_total += len(job_ids)

        if "screen" in stages:
            job_ids = self._db.reset_screening_errors()
            for jid in job_ids:
                self._screening_queue.put(jid)
            if job_ids:
                logger.info("Auto-retry: requeued {} screening-error job(s)", len(job_ids))
                requeued_total += len(job_ids)

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
                requeued_total += len(to_retry)

        return requeued_total

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
