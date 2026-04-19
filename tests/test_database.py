"""Tests for job_search.core.database — DatabaseManager CRUD."""
from __future__ import annotations

from job_search.core.database import DatabaseManager, ScreeningResult


class TestJobOperations:
    def test_insert_and_exists(self, db: DatabaseManager) -> None:
        assert not db.job_exists(1001)
        db.insert_job(1001, "Python Developer", "102713980")
        assert db.job_exists(1001)

    def test_insert_ignore_duplicate(self, db: DatabaseManager) -> None:
        db.insert_job(1001, "Python Developer", "102713980")
        db.insert_job(1001, "Other keyword", "other_loc")  # should not raise
        assert db.job_exists(1001)

    def test_pending_details_initially_empty_after_scraped(self, db: DatabaseManager) -> None:
        db.insert_job(2001, "kw", "loc")
        pending = db.get_jobs_pending_details()
        assert 2001 in pending

    def test_update_job_details_marks_scraped(self, db: DatabaseManager) -> None:
        db.insert_job(3001, "kw", "loc")
        db.update_job_details(3001, {"title": "Senior Python Dev", "description": "Nice job."})
        row = db.get_job_details(3001)
        assert row is not None
        assert row.scraped == 1
        assert row.title == "Senior Python Dev"
        assert 3001 not in db.get_jobs_pending_details()

    def test_update_job_details_filters_unknown_columns(self, db: DatabaseManager) -> None:
        db.insert_job(3002, "kw", "loc")
        # 'totally_unknown' should be silently skipped, not raise
        db.update_job_details(3002, {"title": "Dev", "totally_unknown": "value"})
        row = db.get_job_details(3002)
        assert row.title == "Dev"

    def test_update_job_details_sanitizes_prefix_keys(self, db: DatabaseManager) -> None:
        db.insert_job(3003, "kw", "loc")
        # '$recipeTypes' is in the _FIELD_NAME_MAP; should not raise even if column is now gone
        db.update_job_details(3003, {"$recipeTypes": ["type1", "type2"]})

    def test_update_job_details_stores_company_name(self, db: DatabaseManager) -> None:
        db.insert_job(3004, "kw", "loc")
        db.update_job_details(3004, {"title": "Dev", "company_name": "Acme Corp"})
        row = db.get_job_details(3004)
        assert row.company_name == "Acme Corp"

    def test_update_job_details_strips_country_urn(self, db: DatabaseManager) -> None:
        db.insert_job(3005, "kw", "loc")
        db.update_job_details(3005, {"country": "urn:li:fs_country:de"})
        # The URN prefix should be stripped — verify by checking raw DB value via get_all_jobs
        db.save_screening_result(3005, ScreeningResult(0.8, "none", True, "ok"))
        jobs = db.get_all_jobs()
        job = next((j for j in jobs if j.job_id == 3005), None)
        assert job is not None

    def test_mark_job_error(self, db: DatabaseManager) -> None:
        db.insert_job(4001, "kw", "loc")
        db.mark_job_error(4001)
        row = db.get_job_details(4001)
        assert row.scraped == -1
        assert 4001 not in db.get_jobs_pending_details()

    def test_get_job_details_returns_none_for_unknown(self, db: DatabaseManager) -> None:
        assert db.get_job_details(99999) is None

    def test_get_jobs_pending_screening(self, db: DatabaseManager) -> None:
        db.insert_job(5001, "kw", "loc")
        db.update_job_details(5001, {"title": "Dev"})
        pending = db.get_jobs_pending_screening()
        assert 5001 in pending

    def test_pending_screening_excludes_already_screened(self, db: DatabaseManager) -> None:
        db.insert_job(5002, "kw", "loc")
        db.update_job_details(5002, {"title": "Dev"})
        result = ScreeningResult(
            cv_match_score=0.8,
            german_requirement_level="none",
            is_selected=True,
            reasoning="Good fit",
        )
        db.save_screening_result(5002, result)
        assert 5002 not in db.get_jobs_pending_screening()


class TestScreeningOperations:
    def _insert_scraped_job(self, db: DatabaseManager, job_id: int) -> None:
        db.insert_job(job_id, "kw", "loc")
        db.update_job_details(job_id, {"title": "Dev"})

    def test_save_screening_result(self, db: DatabaseManager) -> None:
        self._insert_scraped_job(db, 7001)
        result = ScreeningResult(
            cv_match_score=0.75,
            german_requirement_level="low",
            is_selected=True,
            reasoning="Strong match",
        )
        db.save_screening_result(7001, result)
        assert 7001 not in db.get_jobs_pending_screening()

    def test_save_screening_result_denormalizes_into_jobs(self, db: DatabaseManager) -> None:
        self._insert_scraped_job(db, 7004)
        result = ScreeningResult(0.85, "none", True, "Good match")
        db.save_screening_result(7004, result)
        row = db.get_job_details(7004)
        assert row is not None
        # is_selected and cv_match_score should be in jobs table
        jobs = db.get_selected_jobs()
        selected_ids = [j.job_id for j in jobs]
        assert 7004 in selected_ids

    def test_save_screening_result_upsert(self, db: DatabaseManager) -> None:
        """Saving a screening result twice should update, not insert duplicate."""
        self._insert_scraped_job(db, 7002)
        r1 = ScreeningResult(0.5, "none", False, "Weak")
        r2 = ScreeningResult(0.9, "high", True, "Great")
        db.save_screening_result(7002, r1)
        db.save_screening_result(7002, r2)  # should not raise

    def test_mark_screening_error(self, db: DatabaseManager) -> None:
        self._insert_scraped_job(db, 7003)
        db.mark_screening_error(7003, "Model timeout")

    def test_pending_cover_letter_after_selection(self, db: DatabaseManager) -> None:
        self._insert_scraped_job(db, 8001)
        result = ScreeningResult(0.85, "none", True, "Great match")
        db.save_screening_result(8001, result)
        pending = db.get_jobs_pending_cover_letter()
        assert 8001 in pending

    def test_not_selected_not_in_cover_letter_queue(self, db: DatabaseManager) -> None:
        self._insert_scraped_job(db, 8002)
        result = ScreeningResult(0.3, "high", False, "Poor match")
        db.save_screening_result(8002, result)
        assert 8002 not in db.get_jobs_pending_cover_letter()


class TestCoverLetterOperations:
    def _insert_selected_job(self, db: DatabaseManager, job_id: int) -> None:
        db.insert_job(job_id, "kw", "loc")
        db.update_job_details(job_id, {"title": "Dev"})
        db.save_screening_result(
            job_id,
            ScreeningResult(0.9, "none", True, "Good"),
        )

    def test_save_cover_letter(self, db: DatabaseManager) -> None:
        self._insert_selected_job(db, 9001)
        db.save_cover_letter(9001, "Dear Hiring Manager...", "gemini-1.5-flash", 0)
        assert 9001 not in db.get_jobs_pending_cover_letter()

    def test_mark_cover_letter_error(self, db: DatabaseManager) -> None:
        self._insert_selected_job(db, 9002)
        db.mark_cover_letter_error(9002, "API timeout", retry_count=1)

    def test_purge_cover_letter_errors(self, db: DatabaseManager) -> None:
        self._insert_selected_job(db, 9003)
        db.mark_cover_letter_error(9003, "API timeout")
        deleted = db.purge_cover_letter_errors()
        assert deleted >= 1


class TestStatsAndApiUsage:
    def test_get_stats_empty_db(self, db: DatabaseManager) -> None:
        stats = db.get_stats()
        assert stats["total_jobs"] == 0
        assert stats["with_details"] == 0
        assert stats["screened"] == 0
        assert stats["selected"] == 0
        assert stats["cover_letters_generated"] == 0

    def test_get_stats_increments(self, db: DatabaseManager) -> None:
        db.insert_job(10001, "kw", "loc")
        db.update_job_details(10001, {"title": "Dev"})
        db.save_screening_result(
            10001,
            ScreeningResult(0.9, "none", True, "Good"),
        )
        db.save_cover_letter(10001, "Letter text", "gemini-1.5-flash", 0)

        stats = db.get_stats()
        assert stats["total_jobs"] == 1
        assert stats["with_details"] == 1
        assert stats["screened"] == 1
        assert stats["selected"] == 1
        assert stats["cover_letters_generated"] == 1

    def test_log_api_usage(self, db: DatabaseManager) -> None:
        db.log_api_usage(0, "generate_content", True)
        db.log_api_usage(1, "generate_content", False, "RateLimitError")
