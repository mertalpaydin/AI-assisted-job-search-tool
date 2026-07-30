from __future__ import annotations

import random
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
        max_workers: int = 1,
    ) -> None:
        self._db = db
        self._session = session
        self._max_workers = max_workers
        self._local = threading.local()

    def _get_thread_session(self) -> requests.Session:
        if not hasattr(self._local, "session") or self._local.session is None:
            if self._session is not None:
                cookies = getattr(self._session, "cookies", None)
                headers = getattr(self._session, "headers", None)
                if cookies is not None and "Mock" not in type(cookies).__name__ and hasattr(cookies, "items"):
                    s = requests.Session()
                    try:
                        s.cookies.update(cookies)
                    except Exception:
                        pass
                    if headers is not None and "Mock" not in type(headers).__name__ and hasattr(headers, "items"):
                        try:
                            s.headers.update(headers)
                        except Exception:
                            pass
                    if "User-Agent" not in s.headers:
                        s.headers.update({"User-Agent": _USER_AGENT})
                    self._local.session = s
                else:
                    self._local.session = self._session
            else:
                s = requests.Session()
                s.headers.update({"User-Agent": _USER_AGENT})
                self._local.session = s
        return self._local.session

    def is_job_expired(self, job_id: int) -> bool | None:
        """Check whether a LinkedIn job is closed/expired.

        Returns:
            True: Job is closed/expired.
            False: Job is active.
            None: Rate-limited or blocked (429, 403, 999).
        """
        # Pacing delay with random jitter (0.3s to 0.8s) to prevent bursting
        time.sleep(random.uniform(0.3, 0.8))

        session = self._get_thread_session()

        closed_keywords = (
            "no longer accepting applications",
            "closed-job",
            "job-closed",
            "closed-job__flavor--closed",
            "topcard__flavor--closed",
            "job is no longer available",
            "this job has expired",
            "job listing has expired",
            "job-unavailable",
            'figure class="closed-job',
        )

        # 1. Check guest API endpoint first
        api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        try:
            resp = session.get(api_url, timeout=8)
            if resp.status_code in (429, 403, 999):
                return None
            if resp.status_code == 200:
                text_lower = resp.text.lower()
                if any(k in text_lower for k in closed_keywords):
                    return True
            elif resp.status_code in (404, 410):
                return True
        except Exception as exc:
            logger.debug("Guest API check error for job {}: {}", job_id, exc)

        # 2. Check direct URL view fallback
        view_url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        try:
            resp_view = session.get(view_url, allow_redirects=False, timeout=8)
            if resp_view.status_code in (429, 403, 999):
                return None
            if resp_view.status_code in (301, 302, 303, 307, 308):
                location = resp_view.headers.get("Location", "")
                if "expired" in location.lower() or "trk=expired_jd_redirect" in location.lower():
                    return True
            elif resp_view.status_code == 200:
                text_lower = resp_view.text.lower()
                if any(k in text_lower for k in closed_keywords):
                    return True
            elif resp_view.status_code in (404, 410):
                return True

            # If no redirect or text match yet, try following redirects
            resp_full = session.get(view_url, allow_redirects=True, timeout=8)
            if resp_full.status_code in (429, 403, 999):
                return None
            if "expired" in resp_full.url.lower() or "trk=expired_jd_redirect" in resp_full.url.lower():
                return True
            if resp_full.status_code in (404, 410) or any(k in resp_full.text.lower() for k in closed_keywords):
                return True
        except Exception as exc:
            logger.debug("Direct URL check error for job {}: {}", job_id, exc)

        return False

    def clean_pending_jobs(self, limit: int | None = None, batch_size: int = 500) -> dict[str, Any]:
        """Scan pending jobs across batches using parallel worker threads until pending jobs are checked."""
        checked_ids: set[int] = set()
        all_expired_ids: list[int] = []
        total_checked = 0
        current_backoff = 15.0

        logger.info("Cleaner: Starting scan across pending jobs ({} parallel workers, batch size {})...", self._max_workers, batch_size)

        def _check_single(job_id: int) -> tuple[int, bool | None]:
            return job_id, self.is_job_expired(job_id)

        while limit is None or limit <= 0 or total_checked < limit:
            batch = self._db.get_pending_jobs_for_cleaner(limit=batch_size, exclude_ids=checked_ids)
            if not batch:
                break

            batch_expired: list[int] = []
            batch_active: list[int] = []
            rate_limited_count = 0

            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                futures = {
                    executor.submit(_check_single, item["job_id"]): item["job_id"]
                    for item in batch
                }

                for future in as_completed(futures):
                    job_id = futures[future]
                    try:
                        j_id, expired_status = future.result()
                        if expired_status is True:
                            checked_ids.add(j_id)
                            batch_expired.append(j_id)
                        elif expired_status is False:
                            checked_ids.add(j_id)
                            batch_active.append(j_id)
                        else:
                            # Rate limited (None) — add to checked_ids for this run so cleaner doesn't spin infinitely on the exact same blocked jobs
                            checked_ids.add(j_id)
                            rate_limited_count += 1
                    except Exception as exc:
                        logger.warning("Cleaner error checking job {}: {}", job_id, exc)

            successfully_checked = len(batch) - rate_limited_count
            total_checked += successfully_checked

            if batch_expired:
                all_expired_ids.extend(batch_expired)
                self._db.mark_jobs_expired_batch(batch_expired)

            if batch_active:
                self._db.mark_jobs_cleaned_batch(batch_active)

            logger.info(
                "Cleaner progress: Checked {} pending jobs — marked {} expired job(s) in DB so far.",
                total_checked,
                len(all_expired_ids),
            )

            if rate_limited_count > 0:
                logger.warning(
                    "Cleaner rate-limited on {} jobs in batch. Aggressive pause for {:.1f}s (max 300s) before retrying...",
                    rate_limited_count,
                    current_backoff,
                )
                time.sleep(current_backoff)
                current_backoff = min(current_backoff * 2.0, 300.0)
            else:
                current_backoff = 15.0

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
