from __future__ import annotations

import queue
import time
from itertools import cycle

import requests
from loguru import logger

from job_search.core.config import Config, LocationConfig
from job_search.core.database import DatabaseManager
from job_search.core.state import ShutdownCoordinator, StateManager
from job_search.scraping.auth import make_headers
from job_search.scraping.models import JobStub

_WORK_TYPE_CODES: dict[str, int] = {"remote": 2, "onsite": 1, "hybrid": 3}

# f_WT is a top-level URL parameter — putting it inside selectedFilters was
# not being respected by LinkedIn's API, causing non-remote jobs to slip through.
_SEARCH_URL_BASE = (
    "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
    "?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-187"
    "&count=100&q=jobSearch"
    "&query=(origin:JOB_SEARCH_PAGE_OTHER_ENTRY,selectedFilters:(sortBy:List(DD))"
    ",keywords:{keyword},locationUnion:(geoId:{geo_id}),spellCorrectionEnabled:true)"
    "&start={start}{wt_filter}"
)

_JOB_CARD_TYPE = "com.linkedin.voyager.dash.jobs.JobPostingCard"


def _parse_search_response(data: dict) -> list[JobStub]:
    stubs: list[JobStub] = []
    for item in data.get("included", []):
        if item.get("$type") != _JOB_CARD_TYPE:
            continue
        if "referenceId" not in item:
            continue
        urn: str = item.get("jobPostingUrn", "")
        if not urn:
            continue
        job_id = int(urn.split(":")[-1])
        title: str | None = item.get("jobPostingTitle")
        sponsored = any(
            x.get("type") == "PROMOTED" for x in item.get("footerItems", [])
        )
        stubs.append(JobStub(job_id=job_id, title=title, sponsored=sponsored))
    return stubs


class SearchWorker:
    """
    Continuously searches LinkedIn for jobs matching configured keywords and
    locations. New job IDs are inserted into the database and pushed onto the
    details queue.
    """

    def __init__(
        self,
        config: Config,
        session: requests.Session,
        db: DatabaseManager,
        state: StateManager,
        shutdown: ShutdownCoordinator,
        details_queue: queue.Queue,
    ) -> None:
        self._config = config
        self._session = session
        self._headers = make_headers(session)
        self._db = db
        self._state = state
        self._shutdown = shutdown
        self._details_queue = details_queue

        # Build cycling iterator over (keyword, location) pairs
        pairs = [
            (kw, loc)
            for kw in config.search.keywords
            for loc in config.search.locations
        ]
        self._pairs: cycle = cycle(pairs)
        self._max_pages = config.search.max_pages

    def run(self) -> None:
        logger.info("Search worker started")
        delay = self._config.search.rate_limits.delay_between_requests

        while not self._shutdown.should_shutdown():
            keyword, location = next(self._pairs)
            try:
                self._search_once(keyword, location)
            except Exception as exc:
                logger.warning("Search error for '{}' / {}: {}", keyword, location.name, exc)

            if self._shutdown.wait(timeout=delay):
                break

        logger.info("Search worker stopped")

    def _search_once(self, keyword: str, location: LocationConfig) -> None:
        total_new = 0
        total_seen = 0

        wt_code = _WORK_TYPE_CODES.get(location.work_type or "", None)
        wt_filter = f"&f_WT={wt_code}" if wt_code else ""

        for page in range(self._max_pages):
            start = page * 100
            url = _SEARCH_URL_BASE.format(
                keyword=keyword, geo_id=location.geo_id, start=start, wt_filter=wt_filter
            )
            resp = self._session.get(url, headers=self._headers, timeout=15)

            if resp.status_code != 200:
                logger.warning(
                    "Search HTTP {} for '{}' @ {} (page {}): {}",
                    resp.status_code, keyword, location.name, page, resp.text,
                )
                break

            stubs = _parse_search_response(resp.json())
            if not stubs:
                break  # No more results

            new_in_page = 0
            for stub in stubs:
                if self._db.job_exists(stub.job_id):
                    continue
                self._db.insert_job(stub.job_id, keyword, location.geo_id)
                self._details_queue.put(stub.job_id)
                self._state.record_new_job()
                new_in_page += 1

            total_new += new_in_page
            total_seen += len(stubs)

            if new_in_page == 0:
                # All jobs on this page already known — no point going deeper
                break

            # Respect rate limits between pages
            if page < self._max_pages - 1 and not self._shutdown.should_shutdown():
                delay = self._config.search.rate_limits.delay_between_requests
                self._shutdown.wait(timeout=delay)

        logger.info(
            "Search '{}' @ {} — {}/{} new jobs ({}p)",
            keyword, location.name, total_new, total_seen,
            min(self._max_pages, (total_seen // 100) + 1),
        )
