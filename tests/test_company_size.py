"""Tests for the two company-size numbers LinkedIn reports.

`staffCount` counts LinkedIn members who list the company as their current
employer. `staffCountRange` is the band the company declares, and is what its
About page shows. They disagree badly and in both directions — gategroup
declares 10,001+ employees and has 2,457 members; Mindrift declares 51-200 and
has 2,300 — so the band drives the size filter and the count is kept as the
secondary figure rather than being thrown away.

The fixtures below are the shapes seen in live responses, including the detail
that put this right: the top band omits `end` entirely instead of sending null.
"""
from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import pytest

from job_search.core.database import DatabaseManager
from job_search.core.state import ShutdownCoordinator
from job_search.scraping.details import DetailsWorker, _extract_company, _staff_range


def _company(name: str, staff_count: int, staff_range: dict | None) -> dict:
    item = {
        "$type": "com.linkedin.voyager.organization.Company",
        "entityUrn": f"urn:li:fs_normalized_company:{name}",
        "name": name,
        "url": f"https://www.linkedin.com/company/{name}",
        "universalName": name,
        "staffCount": staff_count,
    }
    if staff_range is not None:
        item["staffCountRange"] = staff_range
    return item


def _response(company: dict) -> dict:
    return {"data": {"title": "Some Role"}, "included": [company]}


class TestStaffRangeParsing:
    def test_top_band_omits_end(self) -> None:
        """gategroup and every other 10,001+ company come back like this."""
        assert _staff_range({"start": 10001}) == (10001, None)

    def test_bounded_band(self) -> None:
        assert _staff_range({"start": 51, "end": 200}) == (51, 200)

    def test_zero_start_is_kept_not_treated_as_missing(self) -> None:
        """A one-person company reports {start: 0, end: 1}."""
        assert _staff_range({"start": 0, "end": 1}) == (0, 1)

    @pytest.mark.parametrize("raw", [None, {}, "10001+", 42, {"start": "many"}])
    def test_anything_unexpected_yields_nothing(self, raw) -> None:
        assert _staff_range(raw) == (None, None)


class TestCompanyExtraction:
    def test_the_band_is_captured(self) -> None:
        """It was in the field catalog all along but never actually read."""
        company = _extract_company(_response(_company("gategroup", 2457, {"start": 10001})))
        assert company is not None
        assert company.fields["staffCount"] == 2457
        assert company.fields["staffCountRange"] == {"start": 10001}

    def test_a_company_without_a_band_still_parses(self) -> None:
        company = _extract_company(_response(_company("Acme", 12, None)))
        assert company is not None
        assert "staffCountRange" not in company.fields


@pytest.fixture()
def worker_parts(db: DatabaseManager):
    session = MagicMock()
    shutdown = ShutdownCoordinator()
    details_q: queue.Queue = queue.Queue()
    screening_q: queue.Queue = queue.Queue()

    config = MagicMock()
    config.search.rate_limits.delay_between_requests = 0
    config.search.locations = []
    config.search.blocked_companies = []

    worker = DetailsWorker(
        config=config, session=session, db=db, shutdown=shutdown,
        details_queue=details_q, screening_queue=screening_q,
    )
    return worker, session, shutdown, details_q


class TestBandReachesTheDatabase:
    def _fetch(self, worker_parts, db: DatabaseManager, job_id: int, company: dict):
        worker, session, shutdown, details_q = worker_parts
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = _response(company)
        session.get.return_value = resp

        db.insert_job(job_id, "kw", "loc")
        details_q.put(job_id)

        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        details_q.join()
        shutdown.request_shutdown()
        thread.join(timeout=10)

        return db.get_selected_job(job_id)

    def test_both_numbers_are_stored(self, worker_parts, db: DatabaseManager) -> None:
        row = self._fetch(worker_parts, db, 8001,
                          _company("gategroup", 2457, {"start": 10001}))
        assert row is not None
        assert row.company_staff_count == 2457
        assert row.company_staff_range_start == 10001
        assert row.company_staff_range_end is None
        assert row.company_size_label == "10,001+"

    def test_a_missing_band_leaves_the_columns_null(
        self, worker_parts, db: DatabaseManager
    ) -> None:
        row = self._fetch(worker_parts, db, 8002, _company("Acme", 12, None))
        assert row is not None
        assert row.company_staff_count == 12
        assert row.company_staff_range_start is None
        assert row.company_size_label is None
