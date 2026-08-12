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


# Inline batch input is capped at 20MB; leave room for the envelope.
_MAX_INLINE_BYTES = 18 * 1024 * 1024
_MAX_INLINE_REQUESTS = 1000


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
        """Build one inlined batch request.

        The shape here is types.InlinedRequest, not the raw REST envelope: the
        SDK validates every entry and rejects unknown keys, so "key"/"request"
        wrappers are not allowed. The job id travels in ``metadata``, which the
        API echoes back on the matching response, which is how results map home.
        """
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
            "contents": [{"parts": [{"text": user}], "role": "user"}],
            "metadata": {"job_id": str(job_id)},
            "config": {
                "system_instruction": system,
                "temperature": gemini_cfg.temperature,
                "max_output_tokens": gemini_cfg.max_tokens,
            },
        }

    def _chunk(self, requests: list[dict], job_ids: list[int]):
        """Split requests into batches that stay under the inline size ceiling.

        Inline batch input is capped at 20MB. Job descriptions are long, so a
        thousand-job run would otherwise be rejected in one go. Measuring the
        serialised size is cheap next to the API call and beats guessing a
        request count.
        """
        budget = _MAX_INLINE_BYTES
        chunk_reqs: list[dict] = []
        chunk_ids: list[int] = []
        size = 0
        for req, job_id in zip(requests, job_ids):
            req_size = len(json.dumps(req, ensure_ascii=False).encode("utf-8"))
            too_big = size + req_size > budget
            too_many = len(chunk_reqs) >= _MAX_INLINE_REQUESTS
            if chunk_reqs and (too_big or too_many):
                yield chunk_reqs, chunk_ids
                chunk_reqs, chunk_ids, size = [], [], 0
            chunk_reqs.append(req)
            chunk_ids.append(job_id)
            size += req_size
        if chunk_reqs:
            yield chunk_reqs, chunk_ids

    def submit(self, job_ids: list[int]) -> int | None:
        """Submit pending work as one or more batches.

        Returns the first batch id, or None when nothing was accepted. Several
        batches can be open at once; ``collect_all`` polls all of them.
        """
        requests: list[dict] = []
        submitted_ids: list[int] = []
        for job_id in job_ids:
            req = self.build_request(job_id)
            if req is not None:
                requests.append(req)
                submitted_ids.append(job_id)

        if not requests:
            logger.info("Batch submit: nothing eligible")
            return None

        model = self._config.screening.gemini.model
        first_id: int | None = None
        errors: list[str] = []

        for chunk_reqs, chunk_ids in self._chunk(requests, submitted_ids):
            logger.info("Submitting batch of {} screening requests to {}",
                        len(chunk_reqs), model)
            try:
                batch = self._client.batches.create(
                    model=model,
                    src=chunk_reqs,
                    config={"display_name": f"screening-{len(chunk_reqs)}"},
                )
            except Exception as exc:
                # Keep whatever already went out rather than losing it to a
                # later chunk failing: those jobs are genuinely in flight.
                logger.error("Batch submission failed: {}", exc)
                errors.append(str(exc))
                continue

            batch_id = self._db.create_batch_job(batch.name, "screen", chunk_ids)
            first_id = first_id if first_id is not None else batch_id
            logger.info(
                "Batch {} submitted as {} ({} jobs). Results are collected by a later run.",
                batch_id, batch.name, len(chunk_ids),
            )

        if first_id is None:
            raise RuntimeError("no batch was accepted: " + "; ".join(errors[:1]))
        return first_id

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
        unmapped = 0
        for entry in self._iter_responses(batch):
            job_id = _entry_job_id(entry)
            if job_id is None:
                # No positional fallback on purpose: guessing here would write
                # one job's screening verdict onto another job's row.
                unmapped += 1
                continue

            if self._write_result(batch_id, job_id, entry):
                written += 1

        if unmapped:
            logger.warning("Batch {}: {} response(s) carried no job id and were skipped",
                           batch_id, unmapped)

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

    def _iter_responses(self, batch):
        """Yield response entries as plain dicts, whichever shape the SDK returns."""
        dest = getattr(batch, "dest", None)
        inlined = getattr(dest, "inlined_responses", None) if dest else None
        if inlined:
            for item in inlined:
                yield item if isinstance(item, dict) else _to_dict(item)
            return

        # Large batches come back as a JSONL file instead of inline responses.
        name = getattr(dest, "file_name", None) if dest else None
        if not name:
            return
        try:
            blob = self._client.files.download(file=name)
        except Exception as exc:
            logger.error("Could not download batch result file {}: {}", name, exc)
            return
        if isinstance(blob, bytes):
            blob = blob.decode("utf-8", errors="replace")
        for line in blob.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping unparseable line in batch result file {}", name)

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


def _entry_job_id(entry: dict) -> int | None:
    """Read the job id back off a response.

    ``metadata`` is what we set on the request and what the API echoes. The
    JSONL file format uses a top-level ``key`` instead, so both are accepted.
    """
    meta = entry.get("metadata") or {}
    raw = meta.get("job_id") if isinstance(meta, dict) else None
    if raw is None:
        raw = entry.get("key")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None
