"""Tests for the company size backfill.

The backfill exists because company size lives on every job row: repairing it
job-by-job would mean 26,000 requests for 6,500 companies. So the unit of work
is the company, and the properties that matter are that one request repairs
every row that company owns, that an interrupted run resumes without tracking
progress of its own, and that it never writes a sibling company's size.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from job_search.core.database import DatabaseManager
from job_search.scraping.company_backfill import CompanySizeBackfiller, _read_size


def _job(db: DatabaseManager, job_id: int, company: str, universal: str,
         staff_count: int | None = None) -> None:
    db.insert_job(job_id, "kw", "loc")
    fields = {"title": "T", "company_name": company,
              "company_universal_name": universal}
    if staff_count is not None:
        fields["company_staff_count"] = staff_count
    db.update_job_details(job_id, fields)


def _payload(universal: str, staff_count: int, band: dict | None) -> dict:
    company: dict = {
        "$type": "com.linkedin.voyager.organization.Company",
        "entityUrn": f"urn:li:fs_normalized_company:{universal}",
        "name": universal,
        "universalName": universal,
        "staffCount": staff_count,
    }
    if band is not None:
        company["staffCountRange"] = band
    return {"data": {}, "included": [company]}


def _backfiller(db: DatabaseManager, responses: dict) -> CompanySizeBackfiller:
    """A backfiller whose session answers from `responses`, keyed by universalName."""
    session = MagicMock()
    session.cookies = {}

    def _get(url, **_kwargs):
        resp = MagicMock()
        for name, (status, body) in responses.items():
            if f"universalName={name}" in url:
                resp.status_code = status
                resp.json.return_value = body
                return resp
        resp.status_code = 404
        resp.json.return_value = {}
        return resp

    session.get.side_effect = _get
    return CompanySizeBackfiller(db, session, delay=0.0, max_backoff=0.0)


class TestSelectingWork:
    def test_only_companies_missing_a_band_are_listed(self, db: DatabaseManager) -> None:
        _job(db, 1, "Has Band", "hasband")
        _job(db, 2, "No Band", "noband")
        db.save_company_size("hasband", 100, 51, 200)

        names = [c["universal_name"] for c in db.get_companies_missing_size_band()]
        assert names == ["noband"]

    def test_biggest_companies_come_first(self, db: DatabaseManager) -> None:
        """An interrupted run should already have fixed what shows up most."""
        for jid in (1, 2, 3):
            _job(db, jid, "Big", "big")
        _job(db, 4, "Small", "small")

        listed = db.get_companies_missing_size_band()
        assert [c["universal_name"] for c in listed] == ["big", "small"]
        assert listed[0]["job_count"] == 3

    def test_jobs_without_a_universal_name_are_skipped(self, db: DatabaseManager) -> None:
        """There is nothing to look them up by, so they cannot be repaired."""
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "Mystery"})

        assert db.get_companies_missing_size_band() == []


class TestBackfilling:
    def test_one_request_repairs_every_row_of_that_company(
        self, db: DatabaseManager
    ) -> None:
        """The whole reason this walks companies instead of jobs."""
        for jid in (1, 2, 3):
            _job(db, jid, "gategroup", "gategroup", staff_count=2394)

        backfiller = _backfiller(db, {
            "gategroup": (200, _payload("gategroup", 2457, {"start": 10001})),
        })
        summary = backfiller.run()

        assert summary["companies"] == 1
        assert summary["updated_jobs"] == 3
        assert backfiller._session.get.call_count == 1

        for jid in (1, 2, 3):
            row = db.get_selected_job(jid)
            assert row.company_staff_range_start == 10001
            assert row.company_staff_range_end is None
            assert row.company_staff_count == 2457      # drift repaired too
            assert row.company_size_label == "10,001+"

    def test_a_written_company_is_not_fetched_again(self, db: DatabaseManager) -> None:
        """This is what makes an interrupted run resumable with no state."""
        _job(db, 1, "gategroup", "gategroup")
        responses = {"gategroup": (200, _payload("gategroup", 2457, {"start": 10001}))}

        first = _backfiller(db, responses)
        first.run()
        second = _backfiller(db, responses)
        summary = second.run()

        assert second._session.get.call_count == 0
        assert summary["companies"] == 0
        assert summary["remaining"] == 0

    def test_limit_stops_after_that_many_companies(self, db: DatabaseManager) -> None:
        for jid, name in ((1, "aaa"), (2, "bbb"), (3, "ccc")):
            _job(db, jid, name, name)
        responses = {n: (200, _payload(n, 10, {"start": 11, "end": 50}))
                     for n in ("aaa", "bbb", "ccc")}

        summary = _backfiller(db, responses).run(limit=2)

        assert summary["companies"] == 2
        assert summary["remaining"] == 1

    def test_a_company_without_a_band_is_counted_not_written(
        self, db: DatabaseManager
    ) -> None:
        _job(db, 1, "Bandless", "bandless", staff_count=42)

        summary = _backfiller(db, {
            "bandless": (200, _payload("bandless", 42, None)),
        }).run()

        assert summary["no_band"] == 1
        assert summary["companies"] == 0
        assert db.get_selected_job(1).company_staff_range_start is None

    def test_a_dead_company_does_not_stop_the_sweep(self, db: DatabaseManager) -> None:
        _job(db, 1, "Gone", "gone")
        _job(db, 2, "Alive", "alive")

        summary = _backfiller(db, {
            "gone": (404, {}),
            "alive": (200, _payload("alive", 300, {"start": 201, "end": 500})),
        }).run()

        assert summary["companies"] == 1
        assert db.get_selected_job(2).company_staff_range_end == 500

    def test_throttling_pauses_and_leaves_the_company_for_next_time(
        self, db: DatabaseManager
    ) -> None:
        _job(db, 1, "Throttled", "throttled")

        summary = _backfiller(db, {"throttled": (429, {})}).run()

        assert summary["failed"] == 1
        assert summary["companies"] == 0
        assert summary["remaining"] == 1      # still outstanding, will retry

    def test_stop_request_is_honoured_between_companies(
        self, db: DatabaseManager
    ) -> None:
        for jid, name in ((1, "aaa"), (2, "bbb")):
            _job(db, jid, name, name)
        responses = {n: (200, _payload(n, 10, {"start": 11, "end": 50}))
                     for n in ("aaa", "bbb")}

        calls = {"n": 0}

        def stop_after_one() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        summary = _backfiller(db, responses).run(should_stop=stop_after_one)

        assert summary["companies"] == 1
        assert summary["remaining"] == 1


class TestReadingTheResponse:
    def test_the_matching_company_wins_over_a_sibling(self) -> None:
        """A parent or showcase page must not have its size written here."""
        body = {"included": [
            {"universalName": "parent-co", "staffCount": 90000,
             "staffCountRange": {"start": 10001}},
            {"universalName": "gategroup", "staffCount": 2457,
             "staffCountRange": {"start": 10001}},
        ]}
        assert _read_size(body, "gategroup") == (2457, 10001, None)

    def test_ambiguity_is_refused_rather_than_guessed(self) -> None:
        """Writing the wrong company's size is worse than writing none."""
        body = {"included": [
            {"universalName": "other-a", "staffCount": 1, "staffCountRange": {"start": 2, "end": 10}},
            {"universalName": "other-b", "staffCount": 2, "staffCountRange": {"start": 2, "end": 10}},
        ]}
        assert _read_size(body, "wanted") is None

    def test_a_lone_company_is_accepted_without_a_name_match(self) -> None:
        body = {"included": [{"staffCount": 12, "staffCountRange": {"start": 11, "end": 50}}]}
        assert _read_size(body, "whatever") == (12, 11, 50)

    @pytest.mark.parametrize("body", [{}, {"included": []}, {"included": [{"name": "x"}]}])
    def test_nothing_usable_returns_none(self, body) -> None:
        assert _read_size(body, "x") is None
