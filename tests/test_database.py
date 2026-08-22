"""Tests for job_search.core.database — DatabaseManager CRUD."""
from __future__ import annotations

from job_search.core.database import DatabaseManager, ScreeningResult, SelectedJobRow


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
        jobs, _ = db.get_all_jobs()
        job = next((j for j in jobs if j.job_id == 3005), None)
        assert job is not None

    def test_mark_job_error(self, db: DatabaseManager) -> None:
        db.insert_job(4001, "kw", "loc")
        db.mark_job_error(4001)
        row = db.get_job_details(4001)
        assert row.scraped == -1
        assert 4001 not in db.get_jobs_pending_details()

    def test_delete_job_removes_it(self, db: DatabaseManager) -> None:
        db.insert_job(4002, "kw", "loc")
        assert db.job_exists(4002)
        db.delete_job(4002)
        assert not db.job_exists(4002)
        assert db.get_job_details(4002) is None

    def test_delete_job_not_in_pending_details(self, db: DatabaseManager) -> None:
        db.insert_job(4003, "kw", "loc")
        assert 4003 in db.get_jobs_pending_details()
        db.delete_job(4003)
        assert 4003 not in db.get_jobs_pending_details()

    def test_delete_nonexistent_job_does_not_raise(self, db: DatabaseManager) -> None:
        db.delete_job(99998)  # should silently succeed

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
        jobs, _ = db.get_selected_jobs()
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
        assert len(deleted) >= 1


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


class TestKeywordFiltering:
    def test_get_distinct_keywords(self, db: DatabaseManager) -> None:
        db.insert_job(20001, "python", "loc1")
        db.insert_job(20002, "javascript", "loc2")
        db.insert_job(20003, "python", "loc3")
        # should ignore empty/null
        db.insert_job(20004, "", "loc4")
        
        kws = db.get_distinct_keywords()
        assert kws == ["javascript", "python"]

    def test_jobs_filter_by_keyword(self, db: DatabaseManager) -> None:
        db.insert_job(20011, "python", "loc1")
        db.insert_job(20012, "javascript", "loc2")
        
        # update title so they are counted as scraped / detailed
        db.update_job_details(20011, {"title": "Python Dev"})
        db.update_job_details(20012, {"title": "JS Dev"})
        
        # mark as selected for get_selected_jobs
        db.save_screening_result(20011, ScreeningResult(0.9, "none", True, "Good"))
        db.save_screening_result(20012, ScreeningResult(0.9, "none", True, "Good"))

        # Test get_selected_jobs filtering
        jobs_py, count_py = db.get_selected_jobs(keyword_filter="python")
        assert count_py == 1
        assert jobs_py[0].job_id == 20011
        assert jobs_py[0].search_keyword == "python"

        jobs_js, count_js = db.get_selected_jobs(keyword_filter="javascript")
        assert count_js == 1
        assert jobs_js[0].job_id == 20012

        # Test case-insensitive
        jobs_caps, count_caps = db.get_selected_jobs(keyword_filter="PYTHON")
        assert count_caps == 1

        # Test get_all_jobs filtering
        all_py, count_all_py = db.get_all_jobs(keyword_filter="python")
        assert count_all_py == 1
        assert all_py[0].job_id == 20011

        # Test get_company_counts filtering
        db.update_job_details(20011, {"company_name": "PyCorp"})
        db.update_job_details(20012, {"company_name": "JsCorp"})
        
        counts = db.get_company_counts(selected_only=True, keyword_filter="python")
        assert len(counts) == 1
        assert counts[0][0] == "PyCorp"

    def test_include_companies_filtering(self, db: DatabaseManager) -> None:
        db.insert_job(25001, "kw", "loc1")
        db.insert_job(25002, "kw", "loc2")
        db.insert_job(25003, "kw", "loc3")
        db.update_job_details(25001, {"title": "Job A", "company_name": "Alpha Co"})
        db.update_job_details(25002, {"title": "Job B", "company_name": "Beta Co"})
        db.update_job_details(25003, {"title": "Job C", "company_name": "Gamma Co"})
        db.save_screening_result(25001, ScreeningResult(0.9, "none", True, "Pass"))
        db.save_screening_result(25002, ScreeningResult(0.85, "none", True, "Pass"))
        db.save_screening_result(25003, ScreeningResult(0.8, "none", True, "Pass"))

        # Filter by inclusion
        inc_jobs, count = db.get_selected_jobs(include_companies=["Alpha Co"])
        assert count == 1
        assert inc_jobs[0].company_name == "Alpha Co"

        # Filter by multiple inclusion
        inc_jobs_multi, count_multi = db.get_selected_jobs(include_companies=["Alpha Co", "Gamma Co"])
        assert count_multi == 2
        company_names = {j.company_name for j in inc_jobs_multi}
        assert company_names == {"Alpha Co", "Gamma Co"}

        # Filter by none
        inc_jobs_none, count_none = db.get_selected_jobs(include_companies=["__none__"])
        assert count_none == 0
        assert len(inc_jobs_none) == 0


class TestGermanFiltering:
    def test_jobs_filter_by_german_level(self, db: DatabaseManager) -> None:
        db.insert_job(30001, "kw", "loc1")
        db.insert_job(30002, "kw", "loc2")
        db.insert_job(30003, "kw", "loc3")
        db.insert_job(30004, "kw", "loc4")

        db.update_job_details(30001, {"title": "Job 1", "company_name": "Co1"})
        db.update_job_details(30002, {"title": "Job 2", "company_name": "Co2"})
        db.update_job_details(30003, {"title": "Job 3", "company_name": "Co3"})
        db.update_job_details(30004, {"title": "Job 4", "company_name": "Co4"})

        db.save_screening_result(30001, ScreeningResult(0.9, "none", True, "Reason"))
        db.save_screening_result(30002, ScreeningResult(0.8, "low", True, "Reason"))
        db.save_screening_result(30003, ScreeningResult(0.75, "medium", True, "Reason"))
        db.save_screening_result(30004, ScreeningResult(0.7, "high", True, "Reason"))

        # Exact level filter
        none_jobs, none_count = db.get_selected_jobs(german_filter="none")
        assert none_count == 1
        assert none_jobs[0].job_id == 30001

        low_jobs, low_count = db.get_selected_jobs(german_filter="low")
        assert low_count == 1
        assert low_jobs[0].job_id == 30002

        # Cumulative max filters
        max_low_jobs, max_low_count = db.get_selected_jobs(german_filter="max_low")
        assert max_low_count == 2
        assert {j.job_id for j in max_low_jobs} == {30001, 30002}

        max_med_jobs, max_med_count = db.get_selected_jobs(german_filter="max_medium")
        assert max_med_count == 3
        assert {j.job_id for j in max_med_jobs} == {30001, 30002, 30003}

        # get_all_jobs check
        all_high, all_high_count = db.get_all_jobs(german_filter="high")
        assert all_high_count == 1
        assert all_high[0].job_id == 30004

        # company counts check
        co_counts = db.get_company_counts(selected_only=True, german_filter="max_low")
        assert len(co_counts) == 2
        assert {c[0] for c in co_counts} == {"Co1", "Co2"}


def test_get_pending_jobs_for_cleaner_ordering(tmp_path):
    db = DatabaseManager(str(tmp_path / "cleaner_order.db"))
    # Job 1: rejected, created earlier
    db.insert_job(101, keyword="test", location_id="test")
    db.save_screening_result(101, ScreeningResult(0.3, "none", False, "Rejected"))

    # Job 2: selected, created earlier
    db.insert_job(102, keyword="test", location_id="test")
    db.save_screening_result(102, ScreeningResult(0.9, "none", True, "Selected 1"))

    # Job 3: selected, created later
    db.insert_job(103, keyword="test", location_id="test")
    db.save_screening_result(103, ScreeningResult(0.95, "none", True, "Selected 2"))

    pending = db.get_pending_jobs_for_cleaner(limit=10)
    job_ids = [j["job_id"] for j in pending]

    # Selected jobs (103, 102) should come before rejected job (101), with newest selected job (103) first
    assert job_ids == [103, 102, 101]


def test_get_jobs_pending_screening_ordering(tmp_path):
    db = DatabaseManager(str(tmp_path / "screener_order.db"))
    # Job 201: remote (1), scraped
    db.insert_job(201, keyword="test", location_id="test")
    db.update_job_details(201, {"scraped": 1, "workRemoteAllowed": 1})

    # Job 202: non-remote (0), scraped later
    db.insert_job(202, keyword="test", location_id="test")
    db.update_job_details(202, {"scraped": 1, "workRemoteAllowed": 0})

    pending = db.get_jobs_pending_screening()
    # Non-remote job 202 should come first before remote job 201
    assert pending == [202, 201]


def test_easy_apply_cover_letter_exclusion(tmp_path):
    db = DatabaseManager(str(tmp_path / "easy_apply.db"))
    # Easy Apply job
    db.insert_job(301, keyword="test", location_id="test")
    db.update_job_details(301, {"scraped": 1, "applyMethod": '{"easyApplyUrl": "http://example.com"}'})
    db.save_screening_result(301, ScreeningResult(0.9, "none", True, "Pass"))

    # Company website job
    db.insert_job(302, keyword="test", location_id="test")
    db.update_job_details(302, {"scraped": 1, "applyMethod": '{"companyApplyUrl": "http://example.com"}'})
    db.save_screening_result(302, ScreeningResult(0.9, "none", True, "Pass"))

    # In auto mode by default, Easy Apply job 301 is skipped, only 302 included
    pending_cl = db.get_jobs_pending_cover_letter(mode="auto")
    assert pending_cl == [302]

    # If user explicitly approves Easy Apply job 301, it is queued for CL generation in both modes
    db.set_cl_approval(301, 1)
    assert set(db.get_jobs_pending_cover_letter(mode="auto")) == {301, 302}
    assert db.get_jobs_pending_cover_letter(mode="user_approval") == [301]




class TestCompanySizeFilter:
    def test_get_all_jobs_size_buckets(self, db: DatabaseManager) -> None:
        # (job_id, company_staff_count)
        specs = [(1, 5), (2, 150), (3, 500), (4, 3000), (5, 50000), (6, 0)]
        for jid, staff in specs:
            db.insert_job(jid, "kw", "loc")
            db.update_job_details(jid, {"title": "T", "company_name": f"C{jid}",
                                        "company_staff_count": staff})

        def ids(size: str):
            rows, _ = db.get_all_jobs(size_filter=size, limit=100)
            return sorted(r.job_id for r in rows)

        assert ids("micro") == [1]           # 1-10
        assert ids("startup") == [2]         # 11-200
        assert ids("mid") == [3]             # 201-1000
        assert ids("large") == [4]           # 1001-5000
        assert ids("global") == [5]          # 10001+
        assert ids("unknown") == [6]         # 0 / null
        assert len(ids("")) == 6             # no filter returns all

    def test_buckets_never_split_a_linkedin_band(self, db: DatabaseManager) -> None:
        """Every boundary sits on one of LinkedIn's own band edges.

        LinkedIn reports nine bands. If a bucket boundary fell inside one, the
        companies in that band would land in different buckets on no evidence
        at all, since the band is the only size information we have.
        """
        bands = [(0, 1), (2, 10), (11, 50), (51, 200), (201, 500),
                 (501, 1000), (1001, 5000), (5001, 10000), (10001, None)]
        for jid, (start, end) in enumerate(bands, start=1):
            db.insert_job(jid, "kw", "loc")
            db.update_job_details(jid, {"title": "T", "company_name": f"C{jid}",
                                        "company_staff_range_start": start,
                                        "company_staff_range_end": end})

        found = {}
        for size in ("micro", "startup", "mid", "large", "enterprise", "global"):
            rows, _ = db.get_all_jobs(size_filter=size, limit=100)
            for r in rows:
                found[r.job_id] = size

        assert found == {
            1: "micro", 2: "micro",              # 0-1, 2-10
            3: "startup", 4: "startup",          # 11-50, 51-200
            5: "mid", 6: "mid",                  # 201-500, 501-1000
            7: "large",                          # 1001-5000
            8: "enterprise",                     # 5001-10000
            9: "global",                         # 10001+
        }

    def test_several_buckets_can_be_selected_at_once(self, db: DatabaseManager) -> None:
        """"Large and above" is a union of buckets, not a range."""
        specs = [(1, 5), (2, 150), (3, 500), (4, 3000), (5, 7000), (6, 50000)]
        for jid, staff in specs:
            db.insert_job(jid, "kw", "loc")
            db.update_job_details(jid, {"title": "T", "company_name": f"C{jid}",
                                        "company_staff_count": staff})

        def ids(size: str):
            rows, _ = db.get_all_jobs(size_filter=size, limit=100)
            return sorted(r.job_id for r in rows)

        assert ids("large,enterprise,global") == [4, 5, 6]
        assert ids("micro,global") == [1, 6]        # a union no range could express
        assert ids("large") == [4]                  # a single value still works

    def test_selecting_everything_is_the_same_as_no_filter(
        self, db: DatabaseManager
    ) -> None:
        for jid, staff in ((1, 5), (2, 3000)):
            db.insert_job(jid, "kw", "loc")
            db.update_job_details(jid, {"title": "T", "company_name": f"C{jid}",
                                        "company_staff_count": staff})
        every = "micro,startup,mid,large,enterprise,global,unknown"
        assert db.get_all_jobs(size_filter=every, limit=100)[1] == 2

    def test_unknown_bucket_names_are_ignored_not_matched(
        self, db: DatabaseManager
    ) -> None:
        """A stale bookmark should widen the results, never empty them."""
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "C",
                                  "company_staff_count": 3000})

        rows, _ = db.get_all_jobs(size_filter="large,bogus", limit=100)
        assert [r.job_id for r in rows] == [1]
        # Nothing recognisable at all means no size filter rather than no rows.
        assert db.get_all_jobs(size_filter="bogus", limit=100)[1] == 1

    def test_whitespace_and_repeats_are_tolerated(self, db: DatabaseManager) -> None:
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "C",
                                  "company_staff_count": 3000})
        rows, _ = db.get_all_jobs(size_filter=" large , large ,, ", limit=100)
        assert [r.job_id for r in rows] == [1]

    def test_the_top_two_bands_are_no_longer_merged(self, db: DatabaseManager) -> None:
        """A 6,000-person employer and a global corporate are different targets."""
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "Mittelstand",
                                  "company_staff_range_start": 5001,
                                  "company_staff_range_end": 10000})
        db.insert_job(2, "kw", "loc")
        db.update_job_details(2, {"title": "T", "company_name": "Global Corp",
                                  "company_staff_range_start": 10001,
                                  "company_staff_range_end": None})

        ent, _ = db.get_all_jobs(size_filter="enterprise", limit=10)
        glo, _ = db.get_all_jobs(size_filter="global", limit=10)
        assert [r.job_id for r in ent] == [1]
        assert [r.job_id for r in glo] == [2]

    def test_the_declared_band_beats_the_member_count(self, db: DatabaseManager) -> None:
        """The gategroup case: 2,457 LinkedIn members, but 10,001+ declared.

        Bucketing on the member count filed it under "large". The band is what
        the company says about itself, so it belongs in "global".
        """
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "gategroup",
                                  "company_staff_count": 2457,
                                  "company_staff_range_start": 10001,
                                  "company_staff_range_end": None})
        # And the reverse: a crowdsourcing platform with more members listing it
        # than it declares employees.
        db.insert_job(2, "kw", "loc")
        db.update_job_details(2, {"title": "T", "company_name": "Mindrift",
                                  "company_staff_count": 2300,
                                  "company_staff_range_start": 51,
                                  "company_staff_range_end": 200})

        def ids(size: str):
            rows, _ = db.get_all_jobs(size_filter=size, limit=100)
            return sorted(r.job_id for r in rows)

        assert ids("global") == [1]
        assert ids("startup") == [2]
        assert ids("large") == []

    def test_rows_without_a_band_fall_back_to_the_member_count(
        self, db: DatabaseManager
    ) -> None:
        """Everything scraped before the band existed must stay filterable."""
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "Old",
                                  "company_staff_count": 3000})

        rows, _ = db.get_all_jobs(size_filter="large", limit=100)
        assert [r.job_id for r in rows] == [1]

    def test_unknown_needs_both_to_be_absent(self, db: DatabaseManager) -> None:
        db.insert_job(1, "kw", "loc")
        db.update_job_details(1, {"title": "T", "company_name": "NoData"})
        db.insert_job(2, "kw", "loc")
        db.update_job_details(2, {"title": "T", "company_name": "Undisclosed",
                                  "company_staff_count": 0,
                                  "company_staff_range_start": 51,
                                  "company_staff_range_end": 200})

        rows, _ = db.get_all_jobs(size_filter="unknown", limit=100)
        assert [r.job_id for r in rows] == [1]


class TestCompanySizeLabel:
    """What the two numbers are called in the UI."""

    def _row(self, **kwargs):
        db_row = SelectedJobRow(
            job_id=1, title="T", company_name="C", formattedLocation=None,
            jobPostingUrl=None, workRemoteAllowed=None, description=None,
            application_status=None, applied_at=None, cv_match_score=None,
            german_requirement_level=None, is_selected=None,
            screening_reasoning=None, cover_letter_text=None,
            generation_date=None, generation_status=None, **kwargs,
        )
        return db_row

    def test_top_band_renders_with_a_plus(self) -> None:
        row = self._row(company_staff_range_start=10001, company_staff_count=2457)
        assert row.company_size_label == "10,001+"

    def test_bounded_band_renders_as_a_range(self) -> None:
        row = self._row(company_staff_range_start=1001, company_staff_range_end=5000)
        assert row.company_size_label == "1,001–5,000"

    def test_no_band_has_no_label(self) -> None:
        """Old rows must not have their member count passed off as headcount."""
        row = self._row(company_staff_count=2457)
        assert row.company_size_label is None

    def test_startup_badge_follows_the_band_not_the_count(self) -> None:
        # 33 members, but the company declares 51-200: not a startup.
        assert self._row(company_staff_count=33, company_staff_range_start=51,
                         company_staff_range_end=200).is_small_company is False
        # Same member count, declared 11-50: genuinely small.
        assert self._row(company_staff_count=33, company_staff_range_start=11,
                         company_staff_range_end=50).is_small_company is True
        # The unbounded top band is never small, whatever the member count says.
        assert self._row(company_staff_count=1, company_staff_range_start=10001
                         ).is_small_company is False
        # No band at all: fall back to the count.
        assert self._row(company_staff_count=12).is_small_company is True
