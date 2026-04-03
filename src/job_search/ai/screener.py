from __future__ import annotations

import json
import queue
import re
from pathlib import Path

from loguru import logger

from job_search.core.config import Config
from job_search.core.database import DatabaseManager, ScreeningResult
from job_search.core.state import ShutdownCoordinator
from job_search.ai.prompt_manager import PromptManager

_GERMAN_LEVELS = ("none", "low", "medium", "high")


def _parse_screening_json(text: str) -> dict:
    """Extract the first JSON object from model output."""
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in model output: {text[:200]}")
    return json.loads(match.group())


def _apply_criteria(raw: dict, config: Config) -> ScreeningResult:
    """Validate model output and apply configured selection thresholds."""
    criteria = config.screening.criteria

    cv_match = float(raw.get("cv_match_score", 0.0))
    german_level = str(raw.get("german_requirement_level", "none")).lower()
    location_match = bool(raw.get("location_match", False))
    reasoning = str(raw.get("reasoning", ""))

    if german_level not in _GERMAN_LEVELS:
        german_level = "none"

    max_german_idx = _GERMAN_LEVELS.index(criteria.max_german_level)
    german_ok = _GERMAN_LEVELS.index(german_level) <= max_german_idx

    is_selected = (
        cv_match >= criteria.min_cv_match_score
        and german_ok
        and location_match
    )

    return ScreeningResult(
        cv_match_score=cv_match,
        german_requirement_level=german_level,
        location_match=location_match,
        is_selected=is_selected,
        reasoning=reasoning,
    )


class ScreeningWorker:
    """
    Loads a local GGUF model via llama-cpp-python, screens jobs from the
    screening queue, and saves results to the database.

    The model is loaded lazily on first use to avoid blocking startup.
    GPU acceleration is enabled via n_gpu_layers=-1 (all layers on GPU).

    Note: llama-cpp-python must be installed with CUDA support:
        pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121
    """

    def __init__(
        self,
        config: Config,
        db: DatabaseManager,
        shutdown: ShutdownCoordinator,
        screening_queue: queue.Queue,
        cover_letter_queue: queue.Queue,
        prompt_manager: PromptManager,
    ) -> None:
        self._config = config
        self._db = db
        self._shutdown = shutdown
        self._screening_queue = screening_queue
        self._cover_letter_queue = cover_letter_queue
        self._prompts = prompt_manager
        self._llm = None

    def _load_model(self) -> None:
        from llama_cpp import Llama  # type: ignore[import]

        model_cfg = self._config.screening.model
        model_path = Path(model_cfg.path)
        if not model_path.exists():
            raise FileNotFoundError(
                f"GGUF model not found: {model_path}\n"
                "Place your model file in data/models/ and update config/config.yaml."
            )

        logger.info("Loading GGUF model: {}", model_path)
        self._llm = Llama(
            model_path=str(model_path),
            n_gpu_layers=model_cfg.n_gpu_layers,
            n_ctx=model_cfg.n_ctx,
            verbose=False,
        )
        logger.info("Screening model loaded")

    def _infer(self, system_prompt: str, user_prompt: str) -> str:
        """Run chat completion and return the assistant's reply."""
        model_cfg = self._config.screening.model
        response = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=model_cfg.max_new_tokens,
            temperature=model_cfg.temperature,
        )
        return response["choices"][0]["message"]["content"]

    def _screen_job(self, job_id: int) -> None:
        job = self._db.get_job_details(job_id)
        if job is None:
            logger.warning("Job {} not found in DB — skipping screening", job_id)
            return

        system, user = self._prompts.format_screening_prompt(
            job_title=job.title or "",
            company_name=job.company_name,
            job_location=job.formattedLocation,
            remote_allowed=bool(job.workRemoteAllowed),
            job_description=job.description,
        )

        raw_output = self._infer(system, user)

        try:
            raw = _parse_screening_json(raw_output)
            result = _apply_criteria(raw, self._config)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("Screening parse error for job {}: {}", job_id, exc)
            self._db.mark_screening_error(job_id, str(exc))
            return

        self._db.save_screening_result(job_id, result)

        if result.is_selected:
            self._cover_letter_queue.put(job_id)
            logger.info(
                "Job {} SELECTED — cv_match={:.2f}, german={}, location={}",
                job_id, result.cv_match_score, result.german_requirement_level, result.location_match,
            )
        else:
            logger.debug(
                "Job {} rejected — cv_match={:.2f}, german={}, location={}",
                job_id, result.cv_match_score, result.german_requirement_level, result.location_match,
            )

    def run(self) -> None:
        logger.info("Screening worker started")
        self._load_model()

        while not self._shutdown.should_shutdown():
            try:
                job_id: int = self._screening_queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                self._screen_job(job_id)
            except Exception as exc:
                logger.error("Unhandled screening error for job {}: {}", job_id, exc)
                self._db.mark_screening_error(job_id, str(exc))
            finally:
                self._screening_queue.task_done()

        logger.info("Screening worker stopped")
