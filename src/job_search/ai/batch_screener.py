"""Batch screening: submit many jobs at once, collect the results later.

Two phases, because the Batch API is asynchronous:

    submit    pending jobs -> requests -> provider batch -> mark in flight -> exit
    collect   open batches -> poll -> write results -> release jobs -> mark done

The submitting process does not wait. That is the point: screening stops
depending on the laptop staying awake, and costs half as much.

The safety rule that makes concurrent on-demand runs harmless is in
``_write_result``: a result is only written if the job still points at the
batch it came from. Anything else, an abandoned batch or a re-submission, is
discarded rather than allowed to overwrite a fresher answer.
"""
from __future__ import annotations

import json

from google import genai
from google.genai import types as genai_types
from loguru import logger

from job_search.ai.prompt_manager import PromptManager
from job_search.ai.screener import _apply_criteria, _parse_screening_json
from job_search.core.config import Config
from job_search.core.database import DatabaseManager

# States the provider reports. Anything else is treated as still running.
_SUCCESS_STATES = ("JOB_STATE_SUCCEEDED",)
_FAILURE_STATES = ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED")


class BatchScreener:
    """Submits and collects screening batches."""

    def __init__(self, config: Config, db: DatabaseManager, api_key: str,
                 prompt_manager: PromptManager | None = None) -> None:
        self._config = config
        self._db = db
        self._client = genai.Client(api_key=api_key)
        self._prompts = prompt_manager or PromptManager()

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def build_request(self, job_id: int) -> dict | None:
        """Build one batch request, keyed by job_id so results map back trivially."""
        job = self._db.get_job_details(job_id)
        if job is None:
            return None

        system, user = self._prompts.format_screening_prompt(
            job_title=job.title or "",
            company_name=job.company_name,
            job_location=job.formattedLocation,
            remote_allowed=bool(job.workRemoteAllowed),
            job_description=job.description,
        )
        gemini_cfg = self._config.screening.gemini
        return {
            "key": str(job_id),
            "request": {
                "contents": [{"parts": [{"text": user}], "role": "user"}],
                "system_instruction": {"parts": [{"text": system}]},
                "generation_config": {
                    "temperature": gemini_cfg.temperature,
                    "max_output_tokens": gemini_cfg.max_tokens,
                },
            },
        }

    def submit(self, job_ids: list[int]) -> int | None:
        """Submit a batch and return the local batch id, or None if nothing to do."""
        requests = []
        submitted_ids = []
        for job_id in job_ids:
            req = self.build_request(job_id)
            if req is not None:
                requests.append(req)
                submitted_ids.append(job_id)

        if not requests:
            logger.info("Batch submit: nothing eligible")
            return None

        model = self._config.screening.gemini.model
        logger.info("Submitting batch of {} screening requests to {}", len(requests), model)

        try:
            batch = self._client.batches.create(
                model=model,
                src=requests,
                config={"display_name": f"screening-{len(requests)}"},
            )
        except Exception as exc:
            logger.error("Batch submission failed: {}", exc)
            raise

        batch_id = self._db.create_batch_job(batch.name, "screen", submitted_ids)
        logger.info(
            "Batch {} submitted as {} ({} jobs). Results are collected by a later run.",
            batch_id, batch.name, len(submitted_ids),
        )
        return batch_id

    # ------------------------------------------------------------------
    # Collect
    # ------------------------------------------------------------------

    def collect_all(self, stale_after_hours: float = 36.0) -> dict[str, int]:
        """Poll every open batch and write back whatever has finished."""
        summary = {"checked": 0, "collected": 0, "still_running": 0, "failed": 0}

        for row in self._db.get_open_batch_jobs():
            summary["checked"] += 1
            batch_id, name = row["id"], row["provider_job_name"]
            try:
                batch = self._client.batches.get(name=name)
            except Exception as exc:
                logger.warning("Could not read batch {}: {}", name, exc)
                continue

            state = str(getattr(batch.state, "name", batch.state))

            if state in _SUCCESS_STATES:
                written = self._collect_one(batch_id, batch)
                summary["collected"] += written
                logger.info("Batch {} collected: {} result(s) written", batch_id, written)
            elif state in _FAILURE_STATES:
                # Release the jobs so the ordinary screening path retries them.
                self._db.clear_batch_link(self._db.get_batch_job_ids(batch_id))
                self._db.finish_batch_job(batch_id, "failed", error_message=state)
                summary["failed"] += 1
                logger.warning("Batch {} ended as {}; its jobs were released", batch_id, state)
            else:
                summary["still_running"] += 1
                age = row.get("age_hours") or 0
                if age > stale_after_hours:
                    logger.warning(
                        "Batch {} has been {} for {:.1f}h, well past the {}h ceiling. "
                        "Consider abandoning it from the runner page.",
                        batch_id, state, age, stale_after_hours,
                    )

        return summary

    def _collect_one(self, batch_id: int, batch) -> int:
        written = 0
        for entry in self._iter_responses(batch):
            key = entry.get("key")
            if key is None:
                continue
            try:
                job_id = int(key)
            except ValueError:
                continue

            if self._write_result(batch_id, job_id, entry):
                written += 1

        remaining = self._db.get_batch_job_ids(batch_id)
        if remaining:
            # Requests the provider never answered: hand them back to the
            # normal path rather than leaving them stuck in flight.
            self._db.clear_batch_link(remaining)
            logger.info("Batch {}: {} job(s) had no response, released", batch_id, len(remaining))

        state = "succeeded" if written else "partial"
        self._db.finish_batch_job(batch_id, state, collected=written)
        return written

    def _write_result(self, batch_id: int, job_id: int, entry: dict) -> bool:
        """Persist one response, but only if the job still belongs to this batch."""
        if not self._db.job_belongs_to_batch(job_id, batch_id):
            logger.debug("Job {} no longer belongs to batch {}, discarding result",
                         job_id, batch_id)
            return False

        text = self._extract_text(entry)
        if text is None:
            self._db.clear_batch_link([job_id])
            self._db.mark_screening_error(job_id, "batch response contained no text")
            return False

        try:
            result = _apply_criteria(_parse_screening_json(text), self._config)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._db.clear_batch_link([job_id])
            self._db.mark_screening_error(job_id, f"batch parse error: {exc}")
            return False

        self._db.save_screening_result(job_id, result)
        self._db.clear_batch_link([job_id])
        return True

    @staticmethod
    def _iter_responses(batch):
        """Yield response entries as plain dicts, whichever shape the SDK returns."""
        dest = getattr(batch, "dest", None)
        inlined = getattr(dest, "inlined_responses", None) if dest else None
        if inlined:
            for item in inlined:
                yield item if isinstance(item, dict) else _to_dict(item)
            return

        # File-based results arrive as JSONL, one object per line.
        raw = getattr(dest, "file_name", None) if dest else None
        if raw:
            logger.info("Batch results are in file {}; download and re-run collect", raw)

    @staticmethod
    def _extract_text(entry: dict) -> str | None:
        response = entry.get("response") or {}
        for candidate in response.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    return part["text"]
        return None


def _to_dict(obj):
    """Best-effort conversion of an SDK object to a plain dict."""
    for attr in ("to_json_dict", "model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return getattr(obj, "__dict__", {})
