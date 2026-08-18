"""Fill in the declared company size band for jobs scraped before it existed.

Every job row carries its company's size, so the naive repair would refetch
26,000 job postings. There are only ~6,500 distinct companies behind them, and
LinkedIn will answer for a company directly, so this walks the companies
instead: one request fills in every job row that company owns.

Resumable by construction. A company that has been written no longer matches
``get_companies_missing_size_band``, so an interrupted run picks up where it
stopped without tracking any progress of its own. Biggest companies go first,
so stopping early still leaves the lists mostly repaired.
"""
from __future__ import annotations

import time
from urllib.parse import quote

import requests
from loguru import logger

from job_search.core.database import DatabaseManager
from job_search.scraping.auth import make_headers
from job_search.scraping.details import _staff_range

_COMPANY_URL = (
    "https://www.linkedin.com/voyager/api/organization/companies"
    "?q=universalName&universalName={name}"
)

# LinkedIn's ways of saying "slow down". 999 is its own, undocumented one.
_THROTTLED = (429, 403, 999)


class CompanySizeBackfiller:
    """Walks distinct companies, asking each for its declared size band."""

    def __init__(
        self,
        db: DatabaseManager,
        session: requests.Session,
        delay: float = 2.0,
        max_backoff: float = 300.0,
        max_attempts: int = 4,
    ) -> None:
        self._db = db
        self._session = session
        self._headers = make_headers(session)
        self._delay = delay
        self._max_backoff = max_backoff
        self._max_attempts = max_attempts

    # ------------------------------------------------------------------

    def run(self, limit: int | None = None, max_runtime_hours: float | None = None,
            should_stop=None) -> dict[str, int]:
        """Backfill until the work runs out, the limit is hit, or time expires.

        ``should_stop`` is polled between companies so a stop request from the
        web UI or a scheduled run's deadline lands promptly instead of after
        6,500 requests.
        """
        summary = {"companies": 0, "updated_jobs": 0, "no_band": 0,
                   "failed": 0, "remaining": 0}

        companies = self._db.get_companies_missing_size_band(limit=limit)
        if not companies:
            logger.info("Company size backfill: nothing to do")
            return summary

        total_jobs = sum(c["job_count"] for c in companies)
        logger.info(
            "Company size backfill: {} companies covering {} job(s), "
            "roughly {:.1f}h at {}s pacing",
            len(companies), total_jobs, len(companies) * self._delay / 3600, self._delay,
        )

        deadline = (
            time.monotonic() + max_runtime_hours * 3600
            if max_runtime_hours is not None else None
        )
        backoff = 15.0

        for index, company in enumerate(companies):
            if should_stop is not None and should_stop():
                logger.info("Company size backfill: stop requested, exiting cleanly")
                break
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    "Company size backfill: reached max runtime of {:.1f}h. "
                    "Re-run to continue where this stopped.", max_runtime_hours,
                )
                break

            name = company["universal_name"]
            outcome, payload = self._fetch(name)

            if outcome == "throttled":
                # Give the whole sweep a rest, not just this company: being
                # throttled is about the account, not the request.
                logger.warning(
                    "Company size backfill: throttled on '{}'. Pausing {:.0f}s "
                    "(max {:.0f}s) before continuing...", name, backoff, self._max_backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                summary["failed"] += 1
                continue

            backoff = 15.0

            if outcome == "error":
                summary["failed"] += 1
            elif payload is None:
                # Answered, but carries no band. Rare — a live probe found one
                # on 18/18 companies — and usually means the page is gone.
                logger.debug("No size band for company '{}'", name)
                summary["no_band"] += 1
            else:
                staff_count, start, end = payload
                rows = self._db.save_company_size(name, staff_count, start, end)
                summary["companies"] += 1
                summary["updated_jobs"] += rows

            if (index + 1) % 100 == 0:
                logger.info(
                    "Company size backfill: {}/{} companies, {} job(s) updated",
                    index + 1, len(companies), summary["updated_jobs"],
                )

            time.sleep(self._delay)

        summary["remaining"] = len(self._db.get_companies_missing_size_band())
        logger.info(
            "Company size backfill finished: {} companies written, {} job(s) updated, "
            "{} without a band, {} failed, {} companies still to do",
            summary["companies"], summary["updated_jobs"], summary["no_band"],
            summary["failed"], summary["remaining"],
        )
        return summary

    # ------------------------------------------------------------------

    def _fetch(self, universal_name: str) -> tuple[str, tuple | None]:
        """Ask LinkedIn for one company.

        Returns (outcome, payload) where outcome is "ok", "throttled" or
        "error", and payload is (staff_count, range_start, range_end) or None
        when the company answered without a size.
        """
        url = _COMPANY_URL.format(name=quote(universal_name, safe=""))
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._session.get(url, headers=self._headers, timeout=15)
            except requests.RequestException as exc:
                if attempt == self._max_attempts:
                    logger.warning("Company '{}' failed: {}", universal_name, exc)
                    return "error", None
                time.sleep(self._delay)
                continue

            if resp.status_code in _THROTTLED:
                return "throttled", None
            if resp.status_code == 404:
                return "ok", None
            if resp.status_code != 200:
                logger.warning("Company '{}': HTTP {}", universal_name, resp.status_code)
                return "error", None

            try:
                body = resp.json()
            except ValueError:
                logger.warning("Company '{}': unparseable response", universal_name)
                return "error", None

            return "ok", _read_size(body, universal_name)

        return "error", None


def _read_size(body: dict, universal_name: str) -> tuple | None:
    """Pull (staffCount, range start, range end) out of a company response.

    Prefers the entry whose universalName matches what was asked for. The
    response can carry several organisations — a parent, a showcase page — and
    writing a sibling's size onto these jobs would be worse than writing
    nothing.
    """
    candidates = [
        item for item in body.get("included", [])
        if isinstance(item, dict) and ("staffCount" in item or "staffCountRange" in item)
    ]
    data = body.get("data")
    if isinstance(data, dict) and ("staffCount" in data or "staffCountRange" in data):
        candidates.append(data)

    if not candidates:
        return None

    wanted = universal_name.lower()
    exact = [c for c in candidates if str(c.get("universalName", "")).lower() == wanted]
    if exact:
        item = exact[0]
    elif len(candidates) == 1:
        item = candidates[0]
    else:
        logger.debug("Company '{}': {} candidates and no exact match, skipping",
                     universal_name, len(candidates))
        return None

    start, end = _staff_range(item.get("staffCountRange"))
    staff_count = item.get("staffCount")
    if not isinstance(staff_count, int):
        staff_count = None
    if start is None and end is None:
        return None
    return staff_count, start, end
