from __future__ import annotations

import queue
import re
import time
from itertools import cycle

import requests
from loguru import logger

from job_search.core.config import Config, LocationConfig, TitleFilterConfig
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


def _title_passes_filter(title: str, cfg: TitleFilterConfig) -> bool:
    """Return True if the title contains at least one required keyword.

    Uses a negative lookbehind on [a-z] so the keyword must start at a
    word boundary (space, punctuation, or start of string), while still
    matching plural/compound forms like 'engineers' or 'IT-Consultants'.
    Short terms like 'ai' and 'ml' are safely guarded: 'email' and 'html'
    don't match because their preceding character IS [a-z].
    """
    if not cfg.require_any:
        return True  # filter disabled
    t = title.lower()
    return any(
        re.search(r"(?<![a-z])" + re.escape(kw.lower()), t)
        for kw in cfg.require_any
    )


def _parse_search_response(data: dict) -> list[JobStub]:
    included = data.get("included", [])
    type_counts: dict[str, int] = {}
    for item in included:
        t = item.get("$type", "<missing>")
        type_counts[t] = type_counts.get(t, 0) + 1
    logger.debug(
        "Response included {} total items — type breakdown: {}",
        len(included),
        ", ".join(f"{t}={n}" for t, n in sorted(type_counts.items())),
    )

    stubs: list[JobStub] = []
    skipped_no_urn = 0
    for item in included:
        if item.get("$type") != _JOB_CARD_TYPE:
            continue
        urn: str = item.get("jobPostingUrn", "")
        if not urn:
            skipped_no_urn += 1
            continue
        job_id = int(urn.split(":")[-1])
        title: str | None = item.get("jobPostingTitle")
        sponsored = any(
            x.get("type") == "PROMOTED" for x in item.get("footerItems", [])
        )
        stubs.append(JobStub(job_id=job_id, title=title, sponsored=sponsored))

    logger.debug(
        "Parsed {} job stubs from {} JobPostingCard items (skipped: {} no-urn)",
        len(stubs),
        type_counts.get(_JOB_CARD_TYPE, 0),
        skipped_no_urn,
    )
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
        self._cycle_size = len(pairs)
        self._max_pages = config.search.max_pages

    def run(self) -> None:
        logger.info("Search worker started")
        rate_limits = self._config.search.rate_limits
        delay = rate_limits.delay_between_requests
        idle_delay = rate_limits.idle_cycle_delay

        cycle_pos = 0
        cycle_new = 0

        while not self._shutdown.should_shutdown():
            keyword, location = next(self._pairs)
            try:
                new_jobs = self._search_once(keyword, location)
                cycle_new += new_jobs
            except Exception as exc:
                logger.warning("Search error for '{}' / {}: {}", keyword, location.name, exc)

            cycle_pos += 1
            if cycle_pos >= self._cycle_size:
                if cycle_new == 0:
                    logger.debug(
                        "Full cycle complete — no new jobs found, cooling down for {}s",
                        idle_delay,
                    )
                    if self._shutdown.wait(timeout=idle_delay):
                        break
                cycle_pos = 0
                cycle_new = 0
                continue

            if self._shutdown.wait(timeout=delay):
                break

        logger.info("Search worker stopped")

    def _search_once(self, keyword: str, location: LocationConfig) -> int:
        total_new = 0
        total_seen = 0
        consecutive_empty = 0

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
            title_filter = self._config.search.title_filter
            for stub in stubs:
                if self._db.job_exists(stub.job_id):
                    continue
                if stub.title and not _title_passes_filter(stub.title, title_filter):
                    logger.debug("Title filter blocked: {}", stub.title)
                    continue
                self._db.insert_job(stub.job_id, keyword, location.geo_id)
                self._details_queue.put(stub.job_id)
                self._state.record_new_job()
                new_in_page += 1

            total_new += new_in_page
            total_seen += len(stubs)

            if new_in_page == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
            else:
                consecutive_empty = 0

            # Respect rate limits between pages
            if page < self._max_pages - 1 and not self._shutdown.should_shutdown():
                delay = self._config.search.rate_limits.delay_between_requests
                self._shutdown.wait(timeout=delay)

        logger.info(
            "Search '{}' @ {} — {}/{} new jobs ({}p)",
            keyword, location.name, total_new, total_seen,
            min(self._max_pages, (total_seen // 100) + 1),
        )
        return total_new
