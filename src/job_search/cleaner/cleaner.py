from __future__ import annotations

import time
from typing import Any

import requests
from loguru import logger

from job_search.core.database import DatabaseManager

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class JobCleaner:
    """Discovers expired/closed LinkedIn jobs and marks them as 'expired'."""

    def __init__(
        self,
        db: DatabaseManager,
        session: requests.Session | None = None,
    ) -> None:
        self._db = db
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def is_job_expired(self, job_id: int) -> bool:
        """Check whether a LinkedIn job is closed/expired."""
        # 1. Check guest API endpoint first
        api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        try:
            resp = self._session.get(api_url, timeout=10)
            if resp.status_code == 200:
                text_lower = resp.text.lower()
                if (
                    "no longer accepting applications" in text_lower
                    or "closed-job" in text_lower
                    or "job-closed" in text_lower
                ):
                    return True
            elif resp.status_code == 404:
                return True
        except Exception as exc:
            logger.debug("Guest API check error for job {}: {}", job_id, exc)

        # 2. Check direct URL redirect header (allow_redirects=False)
        view_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        try:
            resp_view = self._session.get(view_url, allow_redirects=False, timeout=10)
            if resp_view.status_code in (301, 302, 303, 307, 308):
                location = resp_view.headers.get("Location", "")
                if "expired" in location.lower() or "trk=expired_jd_redirect" in location.lower():
                    return True
        except Exception as exc:
            logger.debug("Direct URL check error for job {}: {}", job_id, exc)

        return False

    def clean_pending_jobs(self, limit: int = 100, delay_between: float = 0.3) -> dict[str, Any]:
        """Scan pending jobs and batch mark expired ones."""
        pending_jobs = self._db.get_pending_jobs_for_cleaner(limit=limit)
        if not pending_jobs:
            logger.info("Cleaner: No pending jobs found in DB.")
            return {"checked": 0, "expired": 0, "expired_ids": []}

        logger.info("Cleaner: Inspecting {} pending jobs for expiration...", len(pending_jobs))
        expired_ids: list[int] = []

        for item in pending_jobs:
            job_id: int = item["job_id"]
            if self.is_job_expired(job_id):
                logger.info("Cleaner: Detected EXPIRED job {}", job_id)
                expired_ids.append(job_id)
            time.sleep(delay_between)

        if expired_ids:
            updated_count = self._db.mark_jobs_expired_batch(expired_ids)
            logger.info("Cleaner: Marked {} jobs as 'expired'.", updated_count)

        return {
            "checked": len(pending_jobs),
            "expired": len(expired_ids),
            "expired_ids": expired_ids,
        }
