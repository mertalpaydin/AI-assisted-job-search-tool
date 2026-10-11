"""
Unit tests for database skill indexing and aggregation queries.
"""
import sqlite3
import pytest

from job_search.core.database import DatabaseManager


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    db = DatabaseManager(str(db_file), check_integrity=False)
    yield db
    db.close()


def test_job_skills_table_created(test_db):
    with test_db._cursor() as cur:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='job_skills'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "job_skills"


def test_index_and_retrieve_job_skills(test_db):
    # Insert a dummy job into jobs table
    with test_db._cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (job_id, title, company_name, scraped, description, application_status)
            VALUES (1001, 'Data Scientist', 'TestCo', 1, 'Requires Python and SQL', 'applied')
        """)

    # Index skills
    skills_to_index = ["Python", "SQL", "A/B Testing"]
    inserted = test_db.index_job_skills(1001, skills_to_index, source="extracted")
    assert inserted == 3

    # Duplicate index should ignore
    inserted_dup = test_db.index_job_skills(1001, ["Python", "Docker"], source="extracted")
    assert inserted_dup == 1  # Only Docker is new

    # Retrieve
    retrieved = test_db.get_skills_for_job(1001)
    assert retrieved == ["A/B Testing", "Docker", "Python", "SQL"]


def test_get_skills_for_jobs_batch(test_db):
    with test_db._cursor() as cur:
        cur.execute("INSERT INTO jobs (job_id, scraped, description) VALUES (2001, 1, 'desc1')")
        cur.execute("INSERT INTO jobs (job_id, scraped, description) VALUES (2002, 1, 'desc2')")

    test_db.index_job_skills(2001, ["Python", "SQL"])
    test_db.index_job_skills(2002, ["Docker", "Kubernetes"])

    mapping = test_db.get_skills_for_jobs_batch([2001, 2002, 2003])
    assert mapping[2001] == ["Python", "SQL"]
    assert mapping[2002] == ["Docker", "Kubernetes"]
    assert mapping[2003] == []


def test_get_skill_stats(test_db):
    with test_db._cursor() as cur:
        # Job 1: applied
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, application_status, created_at, company_name)
            VALUES (3001, 1, 'applied', datetime('now'), 'Company A')
        """)
        # Job 2: skipped
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, application_status, created_at, company_name)
            VALUES (3002, 1, 'skipped', datetime('now'), 'Company B')
        """)

    test_db.index_job_skills(3001, ["Python", "A/B Testing"])
    test_db.index_job_skills(3002, ["Python", "SQL"])

    stats = test_db.get_skill_stats(days=30, min_frequency=1)
    assert stats["total_jobs"] == 2
    assert stats["total_applied"] == 1
    assert stats["total_skipped"] == 1

    skills_dict = {s["skill_name"]: s for s in stats["skills"]}
    assert "Python" in skills_dict
    assert skills_dict["Python"]["all_count"] == 2
    assert skills_dict["Python"]["applied_count"] == 1
    assert skills_dict["Python"]["skipped_count"] == 1

    assert "A/B Testing" in skills_dict
    assert skills_dict["A/B Testing"]["applied_count"] == 1
    assert skills_dict["A/B Testing"]["skipped_count"] == 0

    assert "SQL" in skills_dict
    assert skills_dict["SQL"]["applied_count"] == 0
    assert skills_dict["SQL"]["skipped_count"] == 1


def test_get_skill_time_series(test_db):
    with test_db._cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, application_status, created_at)
            VALUES (4001, 1, 'applied', '2026-05-15 10:00:00')
        """)
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, application_status, created_at)
            VALUES (4002, 1, 'skipped', '2026-05-20 10:00:00')
        """)
        cur.execute("""
            INSERT INTO jobs (job_id, scraped, application_status, created_at)
            VALUES (4003, 1, 'applied', '2026-06-10 10:00:00')
        """)

    test_db.index_job_skills(4001, ["A/B Testing"])
    test_db.index_job_skills(4002, ["A/B Testing"])
    test_db.index_job_skills(4003, ["A/B Testing"])

    ts = test_db.get_skill_time_series("A/B Testing")
    assert len(ts) == 2
    assert ts[0]["month"] == "2026-05"
    assert ts[0]["market_count"] == 2
    assert ts[0]["applied_count"] == 1
    assert ts[0]["skipped_count"] == 1

    assert ts[1]["month"] == "2026-06"
    assert ts[1]["market_count"] == 1
    assert ts[1]["applied_count"] == 1


def test_get_jobs_for_skill(test_db):
    with test_db._cursor() as cur:
        cur.execute("""
            INSERT INTO jobs (job_id, title, company_name, scraped, application_status, description, created_at)
            VALUES (5001, 'Senior AI Engineer', 'TopTech', 1, 'applied', 'Extensive A/B testing work', datetime('now'))
        """)

    test_db.index_job_skills(5001, ["A/B Testing"])

    jobs = test_db.get_jobs_for_skill("A/B Testing", status="applied")
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == 5001
    assert jobs[0]["title"] == "Senior AI Engineer"
    assert "A/B testing" in jobs[0]["snippet"]
