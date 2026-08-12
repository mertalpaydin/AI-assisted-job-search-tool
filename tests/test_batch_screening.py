"""Tests for batch screening bookkeeping.

These cover the parts that protect money and correctness: the in-flight guard
that stops a job being screened twice, and the rule that a late result cannot
overwrite a fresher answer.
"""
from __future__ import annotations

import pytest

from job_search.core.database import DatabaseManager, ScreeningResult


def _scraped_job(db: DatabaseManager, job_id: int) -> None:
    db.insert_job(job_id, "kw", "loc")
    db.update_job_details(job_id, {"title": f"Job {job_id}", "company_name": "ACME",
                                   "description": "d" * 400})


class TestInFlightGuard:
    def test_pending_screening_excludes_batched_jobs(self, db: DatabaseManager) -> None:
        """The single change that prevents paying twice."""
        for jid in (901, 902, 903):
            _scraped_job(db, jid)
        assert set(db.get_jobs_pending_screening()) == {901, 902, 903}

        db.create_batch_job("providers/batches/abc", "screen", [901, 902])
        assert db.get_jobs_pending_screening() == [903]

    def test_releasing_jobs_puts_them_back_in_the_queue(self, db: DatabaseManager) -> None:
        _scraped_job(db, 910)
        db.create_batch_job("providers/batches/def", "screen", [910])
        assert 910 not in db.get_jobs_pending_screening()

        db.clear_batch_link([910])
        assert 910 in db.get_jobs_pending_screening()

    def test_prefiltered_jobs_are_still_excluded(self, db: DatabaseManager) -> None:
        _scraped_job(db, 911)
        db.mark_prefiltered(911, "title:werkstudent")
        assert 911 not in db.get_jobs_pending_screening()


class TestBatchLifecycle:
    def test_create_records_the_batch(self, db: DatabaseManager) -> None:
        for jid in (920, 921):
            _scraped_job(db, jid)
        batch_id = db.create_batch_job("providers/batches/xyz", "screen", [920, 921])

        open_batches = db.get_open_batch_jobs()
        assert len(open_batches) == 1
        assert open_batches[0]["id"] == batch_id
        assert open_batches[0]["request_count"] == 2
        assert open_batches[0]["state"] == "submitted"
        assert set(db.get_batch_job_ids(batch_id)) == {920, 921}

    def test_finish_closes_the_batch(self, db: DatabaseManager) -> None:
        _scraped_job(db, 930)
        batch_id = db.create_batch_job("providers/batches/fin", "screen", [930])
        db.finish_batch_job(batch_id, "succeeded", collected=1)
        assert db.get_open_batch_jobs() == []
        recent = db.get_recent_batch_jobs()
        assert recent[0]["state"] == "succeeded"
        assert recent[0]["collected_count"] == 1

    def test_abandon_releases_every_job(self, db: DatabaseManager) -> None:
        for jid in (940, 941):
            _scraped_job(db, jid)
        batch_id = db.create_batch_job("providers/batches/aba", "screen", [940, 941])

        released = db.abandon_batch_job(batch_id)
        assert released == 2
        assert set(db.get_jobs_pending_screening()) >= {940, 941}
        assert db.get_open_batch_jobs() == []


class TestOwnershipRule:
    """A result is only written if the job still points at that batch."""

    def test_job_belongs_to_its_own_batch(self, db: DatabaseManager) -> None:
        _scraped_job(db, 950)
        batch_id = db.create_batch_job("providers/batches/own", "screen", [950])
        assert db.job_belongs_to_batch(950, batch_id) is True

    def test_abandoned_job_no_longer_belongs(self, db: DatabaseManager) -> None:
        """This is what makes a late arrival after an abandon harmless."""
        _scraped_job(db, 951)
        batch_id = db.create_batch_job("providers/batches/late", "screen", [951])
        db.abandon_batch_job(batch_id)
        assert db.job_belongs_to_batch(951, batch_id) is False

    def test_resubmitted_job_belongs_only_to_the_newer_batch(self, db: DatabaseManager) -> None:
        _scraped_job(db, 952)
        first = db.create_batch_job("providers/batches/first", "screen", [952])
        db.abandon_batch_job(first)
        second = db.create_batch_job("providers/batches/second", "screen", [952])

        assert db.job_belongs_to_batch(952, first) is False
        assert db.job_belongs_to_batch(952, second) is True

    def test_unknown_job_belongs_to_nothing(self, db: DatabaseManager) -> None:
        assert db.job_belongs_to_batch(99999, 1) is False


class TestScreeningStillWorks:
    def test_saving_a_result_is_unaffected(self, db: DatabaseManager) -> None:
        _scraped_job(db, 960)
        batch_id = db.create_batch_job("providers/batches/save", "screen", [960])
        db.save_screening_result(960, ScreeningResult(0.9, "none", True, "ok", archetype="A"))
        db.clear_batch_link([960])

        job = db.get_selected_job(960)
        assert job is not None
        assert job.cv_match_score == pytest.approx(0.9)
        assert job.archetype == "A"
        assert 960 not in db.get_jobs_pending_screening()
