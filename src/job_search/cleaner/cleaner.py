from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    """Discovers expired/closed LinkedIn jobs in parallel and marks them as 'expired'."""

    def __init__(
        self,
        db: DatabaseManager,
        session: requests.Session | None = None,
        max_workers: int = 5,
    ) -> None:
        self._db = db
        self._session = session
        self._max_workers = max_workers
        self._local = threading.local()

    def _get_thread_session(self) -> requests.Session:
        if not hasattr(self._local, "session") or self._local.session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": _USER_AGENT})
            self._local.session = s
        return self._local.session

    def is_job_expired(self, job_id: int) -> bool:
        """Check whether a LinkedIn job is closed/expired."""
        session = self._session or self._get_thread_session()

        # 1. Check guest API endpoint first
        api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        try:
            resp = session.get(api_url, timeout=8)
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
            resp_view = session.get(view_url, allow_redirects=False, timeout=8)
            if resp_view.status_code in (301, 302, 303, 307, 308):
                location = resp_view.headers.get("Location", "")
                if "expired" in location.lower() or "trk=expired_jd_redirect" in location.lower():
                    return True
        except Exception as exc:
            logger.debug("Direct URL check error for job {}: {}", job_id, exc)

        return False

    def clean_pending_jobs(self, limit: int | None = None, batch_size: int = 100) -> dict[str, Any]:
        """Scan all pending jobs continuously across batches using parallel worker threads until all pending jobs are checked."""
        checked_ids: set[int] = set()
        all_expired_ids: list[int] = []
        total_checked = 0

        logger.info("Cleaner: Starting scan across all pending jobs (5 parallel workers)...")

        def _check_single(job_id: int) -> tuple[int, bool]:
            return job_id, self.is_job_expired(job_id)

        while limit is None or limit <= 0 or total_checked < limit:
            batch = self._db.get_pending_jobs_for_cleaner(limit=batch_size, exclude_ids=checked_ids)
            if not batch:
                break

            batch_expired: list[int] = []
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = {
                    executor.submit(_check_single, item["job_id"]): item["job_id"]
                    for item in batch
                }

                for future in as_completed(futures):
                    job_id = futures[future]
                    try:
                        j_id, expired = future.result()
                        checked_ids.add(j_id)
                        if expired:
                            batch_expired.append(j_id)
                    except Exception as exc:
                        logger.warning("Cleaner error checking job {}: {}", job_id, exc)

            total_checked += len(batch)

            if batch_expired:
                all_expired_ids.extend(batch_expired)
                self._db.mark_jobs_expired_batch(batch_expired)
                logger.info(
                    "Cleaner: Batch complete — marked {} expired job(s) in DB. (Total checked: {}/{})",
                    len(batch_expired),
                    total_checked,
                    total_checked + len(batch),
                )
            else:
                logger.info(
                    "Cleaner: Batch complete — checked {} jobs (total: {}). No expired jobs in this batch.",
                    len(batch),
                    total_checked,
                )

        logger.info(
            "Cleaner complete: Processed {} total pending jobs — marked {} as expired.",
            total_checked,
            len(all_expired_ids),
        )
        return {
            "checked": total_checked,
            "expired": len(all_expired_ids),
            "expired_ids": all_expired_ids,
        }
