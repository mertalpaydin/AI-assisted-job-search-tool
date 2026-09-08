from __future__ import annotations

import sqlite3
import pytest
from job_search.core.database import DatabaseManager, is_auto_screen_eligible


def test_is_auto_screen_eligible_language():
    # English + Mid-size (500) -> Eligible
    assert is_auto_screen_eligible(
        company_staff_count=500,
        detected_language="en",
        german_stopword_ratio=0.05,
    )

    # German + Mid-size (500) -> Ineligible (excluded due to German)
    assert not is_auto_screen_eligible(
        company_staff_count=500,
        detected_language="de",
        german_stopword_ratio=0.85,
    )

    # German stopword ratio >= 0.50 -> Ineligible
    assert not is_auto_screen_eligible(
        company_staff_count=500,
        detected_language="en",
        german_stopword_ratio=0.55,
    )


def test_is_auto_screen_eligible_company_size():
    # Micro company (1-10) -> Ineligible
    assert not is_auto_screen_eligible(
        company_staff_range_end=10,
        detected_language="en",
        german_stopword_ratio=0.05,
        min_company_size="mid",
    )

    # Startup company (11-200) -> Ineligible
    assert not is_auto_screen_eligible(
        company_staff_count=150,
        detected_language="en",
        german_stopword_ratio=0.05,
        min_company_size="mid",
    )

    # Mid-size company (201-1000) -> Eligible
    assert is_auto_screen_eligible(
        company_staff_count=350,
        detected_language="en",
        german_stopword_ratio=0.05,
        min_company_size="mid",
    )

    # Large company (1001-5000) -> Eligible
    assert is_auto_screen_eligible(
        company_staff_range_end=5000,
        detected_language="en",
        german_stopword_ratio=0.05,
        min_company_size="mid",
    )

    # Global company (10,001+) -> Eligible
    assert is_auto_screen_eligible(
        company_staff_range_start=10001,
        detected_language="en",
        german_stopword_ratio=0.05,
        min_company_size="mid",
    )

    # Unknown company size with allow_unknown_size=False -> Ineligible
    assert not is_auto_screen_eligible(
        company_staff_count=None,
        detected_language="en",
        german_stopword_ratio=0.05,
        allow_unknown_size=False,
    )

    # Unknown company size with allow_unknown_size=True -> Eligible
    assert is_auto_screen_eligible(
        company_staff_count=None,
        detected_language="en",
        german_stopword_ratio=0.05,
        allow_unknown_size=True,
    )


def test_database_get_jobs_pending_screening_filtering(tmp_path):
    db_file = str(tmp_path / "test_jobs.db")
    db = DatabaseManager(db_file, check_integrity=False)

    # Insert 4 test jobs
    # Job 1: English + Mid size (staff 500) -> Auto eligible
    # Job 2: German + Mid size (staff 500) -> Deferred
    # Job 3: English + Startup (staff 50)  -> Deferred
    # Job 4: English + Mid size + prefiltered -> Ineligible for all
    with db._cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, title, description, company_staff_count,
                              detected_language, german_stopword_ratio)
            VALUES (1, 1, 'Job 1', 'English desc', 500, 'en', 0.05)
        """)
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, title, description, company_staff_count,
                              detected_language, german_stopword_ratio)
            VALUES (2, 1, 'Job 2', 'German desc', 500, 'de', 0.90)
        """)
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, title, description, company_staff_count,
                              detected_language, german_stopword_ratio)
            VALUES (3, 1, 'Job 3', 'English startup', 50, 'en', 0.05)
        """)
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, title, description, company_staff_count,
                              detected_language, german_stopword_ratio, prefilter_reason)
            VALUES (4, 1, 'Job 4', 'Prefiltered', 500, 'en', 0.05, 'employment:Part-time')
        """)

    # auto_only=True should ONLY return Job 1
    pending_auto = db.get_jobs_pending_screening(auto_only=True)
    assert pending_auto == [1]

    # auto_only=False should return Jobs 1, 2, 3 (not 4 because prefilter_reason is set)
    pending_all = db.get_jobs_pending_screening(auto_only=False)
    assert set(pending_all) == {1, 2, 3}
