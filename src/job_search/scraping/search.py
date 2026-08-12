from __future__ import annotations

import queue
import time

import requests
from loguru import logger

from job_search.core.config import Config, KeywordConfig, LocationConfig
from job_search.core.database import DatabaseManager
from job_search.core.prefilter import TitlePrefilter
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


# Title matching now lives in job_search.core.prefilter.TitlePrefilter, which is
# shared with the details stage and returns a reason string instead of a bool.


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

        self._keywords: list[KeywordConfig] = config.search.keywords
        self._locations = config.search.locations
        self._default_max_pages = config.search.max_pages
        self._cycle_index = 0
        self._title_prefilter = TitlePrefilter(config)

    def _pairs_for_cycle(self, index: int) -> list[tuple[KeywordConfig, LocationConfig]]:
        """Keyword/location pairs due in this cycle, honouring each term's tier."""
        return [
            (kw, loc)
            for kw in self._keywords
            if index % kw.tier == 0
            for loc in self._locations
        ]

    def run(self) -> None:
        logger.info("Search worker started")
        if not self._keywords:
            logger.warning("No search keywords configured - search worker exiting")
            return
        rate_limits = self._config.search.rate_limits
        delay = rate_limits.delay_between_requests
        idle_delay = rate_limits.idle_cycle_delay

        while not self._shutdown.should_shutdown():
            pairs = self._pairs_for_cycle(self._cycle_index)
            self._cycle_index += 1

            if not pairs:
                # Nothing due this cycle (all remaining terms are higher-tier).
                if self._shutdown.wait(timeout=delay):
                    break
                continue

            logger.debug(
                "Search cycle {}: {} keyword/location pair(s) due",
                self._cycle_index, len(pairs),
            )

            cycle_new = 0
            for keyword, location in pairs:
                if self._shutdown.should_shutdown():
                    break
                try:
                    cycle_new += self._search_once(keyword, location)
                except Exception as exc:
                    logger.warning(
                        "Search error for '{}' / {}: {}", keyword.term, location.name, exc
                    )
                if self._shutdown.wait(timeout=delay):
                    break

            if cycle_new == 0:
                logger.debug(
                    "Cycle {} complete, no new jobs found, cooling down for {}s",
                    self._cycle_index, idle_delay,
                )
                if self._shutdown.wait(timeout=idle_delay):
                    break

        logger.info("Search worker stopped")

    def _search_once(self, keyword: KeywordConfig, location: LocationConfig) -> int:
        total_new = 0
        total_seen = 0
        consecutive_empty = 0

        wt_code = _WORK_TYPE_CODES.get(location.work_type or "", None)
        wt_filter = f"&f_WT={wt_code}" if wt_code else ""
        max_pages = keyword.max_pages or self._default_max_pages

        for page in range(max_pages):
            start = page * 100
            url = _SEARCH_URL_BASE.format(
                keyword=keyword.term, geo_id=location.geo_id, start=start, wt_filter=wt_filter
            )
            resp = self._session.get(url, headers=self._headers, timeout=15)

            if resp.status_code != 200:
                logger.warning(
                    "Search HTTP {} for '{}' @ {} (page {}): {}",
                    resp.status_code, keyword.term, location.name, page, resp.text,
                )
                break

            stubs = _parse_search_response(resp.json())
            if not stubs:
                break  # No more results

            new_in_page = 0
            for stub in stubs:
                if self._db.job_exists(stub.job_id):
                    continue

                # Rejected titles are still stored, with the rule that rejected
                # them, but never fetched or screened.
                reason = self._title_prefilter.reason(stub.title)
                if reason:
                    self._db.insert_job(
                        stub.job_id, keyword.term, location.geo_id,
                        prefilter_reason=reason, title=stub.title,
                    )
                    logger.debug("Prefiltered ({}): {}", reason, stub.title)
                    continue

                self._db.insert_job(stub.job_id, keyword.term, location.geo_id)
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
            if page < max_pages - 1 and not self._shutdown.should_shutdown():
                delay = self._config.search.rate_limits.delay_between_requests
                self._shutdown.wait(timeout=delay)

        logger.info(
            "Search '{}' @ {} — {}/{} new jobs ({}p)",
            keyword.term, location.name, total_new, total_seen,
            min(max_pages, (total_seen // 100) + 1),
        )
        return total_new
