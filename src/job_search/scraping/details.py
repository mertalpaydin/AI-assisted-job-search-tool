from __future__ import annotations

import json
import queue
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from job_search.core.config import Config
from job_search.core.database import DatabaseManager
from job_search.core.state import ShutdownCoordinator
from job_search.scraping.auth import make_headers
from job_search.scraping.models import CompanyData, ParsedJobDetails

class _JobNotFoundError(Exception):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"Job {job_id} returned 404 — deleted from DB")
        self.job_id = job_id


_DETAILS_URL = (
    "https://www.linkedin.com/voyager/api/jobs/jobPostings/{job_id}"
    "?decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65"
)

_COMPANY_TYPE_SUFFIX = "Company"

# Loaded once at import time from the Phase 0 discovery output
_MAPPINGS_PATH = Path("data/samples/field_mappings.json")


def _load_field_mappings() -> tuple[list[dict], list[dict]]:
    """Return (job_fields, company_fields) from field_mappings.json."""
    with _MAPPINGS_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("job_fields", []), data.get("company_fields", [])


_JOB_FIELDS, _COMPANY_FIELDS = _load_field_mappings()


def _get_nested(obj: dict, dotpath: str) -> Any:
    """Walk a dot-separated path like 'data.description.text' into a dict."""
    for key in dotpath.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _extract_job_fields(response: dict) -> dict[str, Any]:
    """Extract all mapped job fields from the API response."""
    fields: dict[str, Any] = {}
    for mapping in _JOB_FIELDS:
        name = mapping["field_name"]
        path = mapping["json_path"]
        value = _get_nested(response, path)

        # description is a nested object — pull the plain text
        if name == "description" and isinstance(value, dict):
            value = value.get("text")

        if value is not None:
            fields[name] = value

    return fields


def _extract_company(response: dict) -> CompanyData | None:
    """Find the Company entry in the included array and extract its fields."""
    for item in response.get("included", []):
        item_type: str = item.get("$type", "")
        if not item_type.endswith(_COMPANY_TYPE_SUFFIX):
            continue
        company_urn: str | None = item.get("entityUrn")
        if not company_urn:
            continue

        company_fields: dict[str, Any] = {}
        for mapping in _COMPANY_FIELDS:
            name = mapping["field_name"]
            path = mapping["json_path"]
            # Company paths are relative to the item itself (no leading key)
            value = _get_nested(item, path)
            if value is not None:
                company_fields[name] = value

        # Always capture name and url even if not in mappings
        for key in ("name", "url", "staffCount", "universalName"):
            if key in item and key not in company_fields:
                company_fields[key] = item[key]

        return CompanyData(company_urn=company_urn, fields=company_fields)

    return None


def _parse_details_response(job_id: int, response: dict) -> ParsedJobDetails:
    job_fields = _extract_job_fields(response)
    company = _extract_company(response)
    return ParsedJobDetails(job_id=job_id, job_fields=job_fields, company=company)


class DetailsWorker:
    """
    Pulls job IDs from the details queue, fetches full job data from the
    LinkedIn Voyager API, and saves everything to the database before pushing
    job IDs onto the screening queue.
    """

    def __init__(
        self,
        config: Config,
        session: requests.Session,
        db: DatabaseManager,
        shutdown: ShutdownCoordinator,
        details_queue: queue.Queue,
        screening_queue: queue.Queue,
    ) -> None:
        self._config = config
        self._session = session
        self._headers = make_headers(session)
        self._db = db
        self._shutdown = shutdown
        self._details_queue = details_queue
        self._screening_queue = screening_queue
        self._max_errors = 10
        self._error_count = 0
        # Geo IDs configured as remote-only — jobs from these geos that come
        # back non-remote are discarded before reaching the screening queue.
        self._remote_geo_ids: frozenset[str] = frozenset(
            loc.geo_id
            for loc in config.search.locations
            if loc.work_type == "remote"
        )

    def run(self) -> None:
        logger.info("Details worker started")
        delay = self._config.search.rate_limits.delay_between_requests

        while not self._shutdown.should_shutdown():
            try:
                job_id: int = self._details_queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                self._fetch_and_save(job_id)
                self._error_count = 0
            except _JobNotFoundError:
                logger.debug("Job {} no longer exists on LinkedIn — removing from DB", job_id)
                self._db.delete_job(job_id)
            except Exception as exc:
                logger.warning("Details error for job {}: {}", job_id, exc)
                self._error_count += 1
                self._db.mark_job_error(job_id)
                if self._error_count >= self._max_errors:
                    logger.error("Too many consecutive details errors — stopping worker")
                    self._shutdown.request_shutdown()
                    break
            finally:
                self._details_queue.task_done()

            if self._shutdown.wait(timeout=delay):
                break

        logger.info("Details worker stopped")

    def _fetch_and_save(self, job_id: int) -> None:
        url = _DETAILS_URL.format(job_id=job_id)
        resp = self._session.get(url, headers=self._headers, timeout=15)

        if resp.status_code == 404:
            raise _JobNotFoundError(job_id)
        if resp.status_code != 200:
            raise RuntimeError(
                f"HTTP {resp.status_code} for job {job_id}: {resp.text}"
            )

        parsed = _parse_details_response(job_id, resp.json())

        if parsed.company:
            cf = parsed.company.fields
            parsed.job_fields["company_name"] = cf.get("name")
            parsed.job_fields["company_url"] = cf.get("url")
            parsed.job_fields["company_staff_count"] = cf.get("staffCount")
            parsed.job_fields["company_universal_name"] = cf.get("universalName")

        self._db.update_job_details(job_id, parsed.job_fields)

        # If this job came from a remote-only geo search but LinkedIn says it's
        # not remote, the API filter didn't apply correctly — discard it.
        if self._remote_geo_ids:
            geo_id, work_remote = self._db.get_job_remote_info(job_id)
            if geo_id in self._remote_geo_ids and not work_remote:
                logger.debug(
                    "Job {} is not remote despite remote-geo search — discarding", job_id
                )
                self._db.delete_job(job_id)
                return

        self._screening_queue.put(job_id)
        logger.debug("Details saved for job {}", job_id)
