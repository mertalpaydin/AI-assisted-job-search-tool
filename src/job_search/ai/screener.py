from __future__ import annotations

import json
import queue
import re
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from loguru import logger

from job_search.core.config import Config
from job_search.core.database import ARCHETYPES, DatabaseManager, ScreeningResult
from job_search.core.state import ShutdownCoordinator
from job_search.ai.prompt_manager import PromptManager
from job_search.utils.api_rotation import GeminiAPIRotator

_GERMAN_LEVELS = ("none", "low", "medium", "high")

# Response schema for Gemini's structured output. With this set the API returns
# syntactically valid JSON by construction: no code fences, no missing braces,
# nothing for a regex to get wrong. Property order matters — it is the order
# the model fills them in, and the prompt deliberately asks for the reasoning
# first so the score follows from it rather than the other way round.
SCREENING_RESPONSE_SCHEMA: dict = {
    "type": "OBJECT",
    "properties": {
        "reasoning": {"type": "STRING"},
        "german_requirement_level": {
            "type": "STRING",
            "enum": list(_GERMAN_LEVELS),
        },
        "cv_match_score": {"type": "NUMBER"},
        "archetype": {"type": "STRING", "enum": list(ARCHETYPES)},
    },
    "required": [
        "reasoning", "german_requirement_level", "cv_match_score", "archetype",
    ],
    "propertyOrdering": [
        "reasoning", "german_requirement_level", "cv_match_score", "archetype",
    ],
}

# Archetypes the screener is allowed to emit. Anything else is normalised to
# "none" rather than trusted, so a hallucinated label cannot reach the database.
_ARCHETYPE_ALIASES = {a.lower(): a for a in ARCHETYPES}


# Fenced blocks the model wraps its answer in: ```json ... ``` or ``` ... ```
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _first_json_object(text: str) -> str | None:
    """Return the first complete, brace-balanced JSON object in text.

    The previous non-greedy ``\\{.*?\\}`` stopped at the first closing brace,
    so a response containing any nested object would have been silently cut
    short and parsed into the wrong shape — worse than failing outright.
    Counting braces (and ignoring those inside strings) keeps whole objects
    whole.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _parse_screening_json(text: str) -> dict:
    """Extract the screening object from model output.

    Structured output makes this a formality, but it stays as the safety net
    for the free-text paths and for any model that decorates its answer. It
    handles, in order: a bare object, a fenced block, and an object whose
    opening brace never arrived — which is exactly how gemini-3.5-flash-lite
    failed, emitting ```json then the fields then a closing brace.
    """
    if not text or not text.strip():
        raise ValueError("Model returned an empty response")

    candidate = _first_json_object(text)
    if candidate is not None:
        return json.loads(candidate)

    # No opening brace anywhere. If what is left looks like the *inside* of an
    # object, supply the brace the model forgot rather than discarding a
    # perfectly good answer.
    stripped = _FENCE.sub("", text.strip()).strip()
    if stripped.endswith("}") and '"' in stripped:
        try:
            return json.loads("{" + stripped)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No JSON found in model output: {text[:400]}")


def threshold_for(archetype: str | None, criteria) -> float:
    """Minimum match score for one role family.

    One global threshold says every family is equally wanted. They are not: a
    junior AI engineering role is worth reading even at a mediocre score
    because it moves in the right direction, while a pure procurement role has
    to be clearly good before it earns a place on the list.

    Falls back to the global min_cv_match_score for any family without an
    override, which includes "none", so an unclassified job is never held to a
    bar nobody chose for it.
    """
    overrides = getattr(criteria, "min_cv_match_score_by_archetype", None) or {}
    key = str(archetype or "none").strip().upper()
    for family, value in overrides.items():
        if str(family).strip().upper() == key:
            return float(value)
    return float(criteria.min_cv_match_score)


def _apply_criteria(raw: dict, config: Config) -> ScreeningResult:
    """Validate model output and apply configured selection thresholds."""
    criteria = config.screening.criteria

    cv_match = float(raw.get("cv_match_score", 0.0))
    german_level = str(raw.get("german_requirement_level", "none")).lower()
    reasoning = str(raw.get("reasoning", ""))

    # Accept "A", "a", "none", or a labelled form such as "A. Procurement x AI".
    # The leading letter is only taken when it is followed by a non-letter, so
    # prose like "Family A" is rejected rather than silently read as "F".
    raw_archetype = str(raw.get("archetype", "") or "").strip().lower()
    archetype = _ARCHETYPE_ALIASES.get(raw_archetype)
    if archetype is None:
        match = re.match(r"^([a-f])(?![a-z])", raw_archetype)
        if match:
            archetype = _ARCHETYPE_ALIASES.get(match.group(1))
    if archetype is None:
        archetype = "none"

    if german_level not in _GERMAN_LEVELS:
        german_level = "none"

    max_german_idx = _GERMAN_LEVELS.index(criteria.max_german_level)
    german_ok = _GERMAN_LEVELS.index(german_level) <= max_german_idx

    is_selected = cv_match >= threshold_for(archetype, criteria) and german_ok

    return ScreeningResult(
        cv_match_score=cv_match,
        german_requirement_level=german_level,
        is_selected=is_selected,
        reasoning=reasoning,
        archetype=archetype,
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
            employment_status=job.formattedEmploymentStatus,
            experience_level=job.formattedExperienceLevel,
            job_functions=job.formattedJobFunctions,
            industries=job.formattedIndustries,
            company_staff_count=job.company_staff_count,
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
            if self._config.cover_letter.mode == "auto":
                self._cover_letter_queue.put(job_id)
            logger.info(
                "Job {} SELECTED [{}] - cv_match={:.2f}, german={} (cl_mode={})",
                job_id, result.archetype, result.cv_match_score,
                result.german_requirement_level, self._config.cover_letter.mode,
            )
        else:
            logger.debug(
                "Job {} rejected — cv_match={:.2f}, german={}",
                job_id, result.cv_match_score, result.german_requirement_level,
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


class GeminiScreeningWorker:
    """
    Screens jobs using the Gemini API instead of a local GGUF model.

    Designed to run as one of N concurrent threads. Each instance shares a
    single GeminiAPIRotator for fair round-robin key rotation and rate limiting.

    NOTE: this used to say that omitting thinking_config disables thinking.
    That was true of the 2.x models it was written for. Gemini 3.x thinks by
    default and charges those tokens against max_output_tokens, which is how a
    512-token budget ended up split between hidden reasoning and an answer that
    then arrived truncated. The budget is sized for both now; see
    ScreeningConfig.gemini.max_tokens.
    """

    def __init__(
        self,
        config: Config,
        db: DatabaseManager,
        shutdown: ShutdownCoordinator,
        screening_queue: queue.Queue,
        cover_letter_queue: queue.Queue,
        prompt_manager: PromptManager,
        rotator: GeminiAPIRotator,
        worker_id: int = 0,
    ) -> None:
        self._config = config
        self._db = db
        self._shutdown = shutdown
        self._screening_queue = screening_queue
        self._cover_letter_queue = cover_letter_queue
        self._prompts = prompt_manager
        self._rotator = rotator
        self._worker_id = worker_id

    def _infer(self, system_prompt: str, user_prompt: str) -> str:
        """Blocking Gemini API call with rotator-based key selection."""
        gemini_cfg = self._config.screening.gemini
        key_idx, api_key = self._rotator.get_next_available_key()
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=gemini_cfg.model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=gemini_cfg.temperature,
                    max_output_tokens=gemini_cfg.max_tokens,
                    # Optional[int] in the SDK, so None is a valid "unset".
                    seed=gemini_cfg.seed,
                    # Structured output: the API guarantees valid JSON rather
                    # than us fishing an object out of prose. Removes the whole
                    # class of "model wrapped it in a fence and dropped the
                    # opening brace" failure.
                    response_mime_type="application/json",
                    response_schema=SCREENING_RESPONSE_SCHEMA,
                ),
            )
            self._rotator.record_success(key_idx)
            self._db.log_api_usage(key_idx, "screening", success=True)
            return response.text
        except Exception as exc:
            self._rotator.record_error(key_idx, type(exc).__name__)
            self._db.log_api_usage(
                key_idx, "screening", success=False, error_type=type(exc).__name__
            )
            raise

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
            employment_status=job.formattedEmploymentStatus,
            experience_level=job.formattedExperienceLevel,
            job_functions=job.formattedJobFunctions,
            industries=job.formattedIndustries,
            company_staff_count=job.company_staff_count,
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
            if self._config.cover_letter.mode == "auto":
                self._cover_letter_queue.put(job_id)
            logger.info(
                "Job {} SELECTED [{}] (gemini-worker-{}) - cv_match={:.2f}, german={} (cl_mode={})",
                job_id, result.archetype, self._worker_id, result.cv_match_score,
                result.german_requirement_level, self._config.cover_letter.mode,
            )
        else:
            logger.debug(
                "Job {} rejected (gemini-worker-{}) — cv_match={:.2f}, german={}",
                job_id, self._worker_id, result.cv_match_score,
                result.german_requirement_level,
            )

    def run(self) -> None:
        logger.info("Gemini screening worker-{} started", self._worker_id)

        while not self._shutdown.should_shutdown():
            try:
                job_id: int = self._screening_queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                self._screen_job(job_id)
            except Exception as exc:
                logger.error(
                    "Unhandled Gemini screening error for job {} (worker-{}): {}",
                    job_id, self._worker_id, exc,
                )
                self._db.mark_screening_error(job_id, str(exc))
            finally:
                self._screening_queue.task_done()

        logger.info("Gemini screening worker-{} stopped", self._worker_id)


def screen_single_job(
    job_id: int,
    config: Config,
    db: DatabaseManager,
    api_key: str | None = None,
    prompt_manager: PromptManager | None = None,
) -> ScreeningResult:
    """Screen one job synchronously on demand using Gemini."""
    job = db.get_job_details(job_id)
    if job is None:
        raise ValueError(f"Job {job_id} not found in database")
    if not job.description:
        raise ValueError(f"Job {job_id} has no scraped description")

    prompts = prompt_manager or PromptManager()
    system, user = prompts.format_screening_prompt(
        job_title=job.title or "",
        company_name=job.company_name,
        job_location=job.formattedLocation,
        remote_allowed=bool(job.workRemoteAllowed),
        job_description=job.description,
        employment_status=job.formattedEmploymentStatus,
        experience_level=job.formattedExperienceLevel,
        job_functions=job.formattedJobFunctions,
        industries=job.formattedIndustries,
        company_staff_count=job.company_staff_count,
    )

    if not api_key:
        from job_search.core.config import load_secrets
        keys = load_secrets().gemini_api_keys
        if not keys:
            raise RuntimeError("No Gemini API keys configured in config/.env")
        api_key = keys[0]

    client = genai.Client(api_key=api_key)
    gemini_cfg = config.screening.gemini

    response = client.models.generate_content(
        model=gemini_cfg.model,
        contents=user,
        config=genai_types.GenerateContentConfig(
            system_instruction=system,
            temperature=gemini_cfg.temperature,
            max_output_tokens=gemini_cfg.max_tokens,
            seed=gemini_cfg.seed,
            response_mime_type="application/json",
            response_schema=SCREENING_RESPONSE_SCHEMA,
        ),
    )
    raw = _parse_screening_json(response.text)
    result = _apply_criteria(raw, config)

    # Save to database and clear any existing prefilter_reason or batch_job_id
    db.save_screening_result(job_id, result)
    with db._cursor() as cur:
        cur.execute(
            "UPDATE jobs SET prefilter_reason = NULL, batch_job_id = NULL WHERE job_id = ?",
            (job_id,),
        )
    return result

