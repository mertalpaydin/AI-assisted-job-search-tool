"""
Tests for the Skills Intelligence web dashboard and A/B Testing Lab routes.
"""
from __future__ import annotations

import pytest
from job_search.core.database import DatabaseManager
from job_search.web.app import init_app


@pytest.fixture()
def client(db: DatabaseManager):
    app = init_app(db)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def setup_sample_jobs_and_skills(db: DatabaseManager):
    """Seed test database with jobs and skills."""
    with db._cursor() as cur:
        # Job 1: applied with A/B Testing, Python
        cur.execute("""
            INSERT INTO jobs (job_id, title, company_name, scraped, application_status, created_at, description, search_keyword)
            VALUES (8001, 'Data Scientist', 'Alpha Corp', 1, 'applied', '2026-05-15 10:00:00', 'Requires A/B Testing and Python', 'Data Science')
        """)
        # Job 2: skipped with SQL, Python
        cur.execute("""
            INSERT INTO jobs (job_id, title, company_name, scraped, application_status, created_at, description, search_keyword)
            VALUES (8002, 'Analytics Lead', 'Beta Ltd', 1, 'skipped', '2026-06-15 10:00:00', 'Requires SQL and Python', 'Data Science')
        """)
        # Job 3: pending/unscreened with A/B Testing
        cur.execute("""
            INSERT INTO jobs (job_id, title, company_name, scraped, application_status, created_at, description, search_keyword)
            VALUES (8003, 'Growth Specialist', 'Gamma Inc', 1, NULL, '2026-07-15 10:00:00', 'Focus on A/B Testing and experimentation', 'Growth')
        """)

    db.index_job_skills(8001, ["A/B Testing", "Python"])
    db.index_job_skills(8002, ["SQL", "Python"])
    db.index_job_skills(8003, ["A/B Testing", "Experimentation"])


def test_skills_dashboard_route(db: DatabaseManager, client) -> None:
    setup_sample_jobs_and_skills(db)

    resp = client.get("/skills")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "Skills Intelligence & A/B Testing Lab" in html
    assert "A/B Testing" in html
    assert "Python" in html
    assert "Skill Gap Leaderboard" in html
    assert "Longitudinal Time Trends" in html
    assert "Interactive A/B Testing Lab" in html


def test_skills_dashboard_filtering_and_sorting(db: DatabaseManager, client) -> None:
    setup_sample_jobs_and_skills(db)

    # Filter by text search query
    resp = client.get("/skills?q=SQL&min_count=1")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "SQL" in html

    # Filter by min frequency
    resp = client.get("/skills?min_count=10")
    assert resp.status_code == 200

    # Sort parameter
    resp = client.get("/skills?sort=market_desc&min_count=1")
    assert resp.status_code == 200


def test_api_skill_time_series(db: DatabaseManager, client) -> None:
    setup_sample_jobs_and_skills(db)

    resp = client.get("/api/skills/time-series?skill=A/B+Testing")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["skill"] == "A/B Testing"
    assert isinstance(data["timeline"], list)
    assert len(data["timeline"]) >= 1
    month_data = data["timeline"][0]
    assert "month" in month_data
    assert "market_count" in month_data
    assert "market_pct" in month_data


def test_api_skill_ab_test(db: DatabaseManager, client) -> None:
    setup_sample_jobs_and_skills(db)

    resp = client.get("/api/skills/test?skill=A/B+Testing")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["skill_name"] == "A/B Testing"
    assert "z_stat" in data
    assert "p_value" in data
    assert "ci_lower_pct" in data
    assert "ci_upper_pct" in data
    assert "contingency" in data
    assert "applied_with" in data["contingency"]
    assert "skipped_with" in data["contingency"]


def test_api_skill_jobs_drilldown(db: DatabaseManager, client) -> None:
    setup_sample_jobs_and_skills(db)

    # Drilldown for all jobs with A/B Testing
    resp = client.get("/api/skills/jobs?skill=A/B+Testing")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["skill"] == "A/B Testing"
    assert len(data["jobs"]) == 2
    titles = [j["title"] for j in data["jobs"]]
    assert "Data Scientist" in titles
    assert "Growth Specialist" in titles

    # Drilldown filtered to applied
    resp_applied = client.get("/api/skills/jobs?skill=A/B+Testing&status=applied")
    assert resp_applied.status_code == 200
    data_app = resp_applied.get_json()
    assert len(data_app["jobs"]) == 1
    assert data_app["jobs"][0]["title"] == "Data Scientist"
