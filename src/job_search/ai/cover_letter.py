from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

from google import genai
from google.genai import types as genai_types
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from job_search.core.config import Config
from job_search.core.database import DatabaseManager
from job_search.core.state import ShutdownCoordinator
from job_search.ai.prompt_manager import PromptManager
from job_search.utils.api_rotation import GeminiAPIRotator


class _RateLimitError(Exception):
    pass


class _TemporaryError(Exception):
    pass


def _classify_exception(exc: Exception) -> Exception:
    """Re-raise API exceptions as retryable or fatal."""
    msg = str(exc).lower()
    if "quota" in msg or "rate" in msg or "429" in msg:
        return _RateLimitError(str(exc))
    if "503" in msg or "500" in msg or "timeout" in msg:
        return _TemporaryError(str(exc))
    return exc


class CoverLetterWorker:
    """
    Generates cover letters for selected jobs using the Gemini API.

    Runs an asyncio event loop in a dedicated thread. Multiple async tasks
    process jobs from the cover_letter queue concurrently, with per-key rate
    limiting and exponential backoff retries.
    """

    def __init__(
        self,
        config: Config,
        db: DatabaseManager,
        shutdown: ShutdownCoordinator,
        cover_letter_queue: queue.Queue,
        prompt_manager: PromptManager,
        api_keys: list[str],
    ) -> None:
        self._config = config
        self._db = db
        self._shutdown = shutdown
        self._queue = cover_letter_queue
        self._prompts = prompt_manager
        self._rotator = GeminiAPIRotator(
            api_keys,
            requests_per_minute=config.cover_letter.rate_limits.requests_per_minute,
        )
        self._cl_cfg = config.cover_letter

    async def _call_gemini(
        self, api_key: str, system_prompt: str, user_prompt: str
    ) -> str:
        """Single Gemini API call (run in executor to avoid blocking event loop)."""
        loop = asyncio.get_running_loop()

        def _sync_call() -> str:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=self._cl_cfg.model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=self._cl_cfg.temperature,
                    max_output_tokens=self._cl_cfg.max_tokens,
                ),
            )
            return response.text

        return await loop.run_in_executor(None, _sync_call)

    async def _generate_for_job(self, job_id: int) -> None:
        job = self._db.get_job_details(job_id)
        if job is None:
            logger.warning("Job {} not found — skipping cover letter", job_id)
            return

        system, user = self._prompts.format_cover_letter_prompt(
            job_title=job.title or "",
            company_name=job.company_name,
            job_location=job.formattedLocation,
            job_description=job.description,
        )

        cl_cfg = self._cl_cfg
        max_retries = cl_cfg.rate_limits.max_retries
        retry_delay = cl_cfg.rate_limits.retry_delay

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_retries),
                wait=wait_exponential(multiplier=2, min=retry_delay, max=retry_delay * 8),
                retry=retry_if_exception_type((_RateLimitError, _TemporaryError)),
                reraise=True,
            ):
                with attempt:
                    key_idx, api_key = self._rotator.get_next_available_key()
                    try:
                        text = await self._call_gemini(api_key, system, user)
                        self._rotator.record_success(key_idx)
                        self._db.log_api_usage(key_idx, "cover_letter", success=True)
                    except Exception as exc:
                        classified = _classify_exception(exc)
                        self._rotator.record_error(key_idx, type(classified).__name__)
                        self._db.log_api_usage(
                            key_idx, "cover_letter", success=False,
                            error_type=type(classified).__name__,
                        )
                        raise classified from exc

        except Exception as exc:
            logger.error("Cover letter failed for job {} after {} retries: {}", job_id, max_retries, exc)
            self._db.mark_cover_letter_error(job_id, str(exc), retry_count=max_retries)
            return

        self._db.save_cover_letter(job_id, text, cl_cfg.model, key_idx)
        logger.info("Cover letter generated for job {} ({})", job_id, job.title)

    async def _worker_loop(self) -> None:
        """Async loop: drain the queue until shutdown."""
        tasks: set[asyncio.Task] = set()
        max_concurrent = self._config.concurrency.max_cover_letter_workers

        while not self._shutdown.should_shutdown():
            # Launch up to max_concurrent tasks
            while len(tasks) < max_concurrent:
                try:
                    job_id: int = self._queue.get_nowait()
                except queue.Empty:
                    break
                task = asyncio.create_task(self._generate_for_job(job_id))
                task.add_done_callback(lambda t: (tasks.discard(t), self._queue.task_done()))
                tasks.add(task)

            if tasks:
                _, tasks_set = await asyncio.wait(
                    tasks, timeout=1.0, return_when=asyncio.FIRST_COMPLETED
                )
                tasks = set(tasks_set)
            else:
                await asyncio.sleep(1.0)

        # Drain remaining tasks on shutdown
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def run(self) -> None:
        """Entry point: runs the async event loop in the calling thread."""
        logger.info("Cover letter worker started")
        asyncio.run(self._worker_loop())
        logger.info("Cover letter worker stopped")
