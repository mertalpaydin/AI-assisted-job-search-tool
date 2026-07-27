"""Tests for web UI routes including German level filtering."""
from __future__ import annotations

import pytest
from job_search.core.database import DatabaseManager, ScreeningResult
from job_search.web.app import init_app


@pytest.fixture()
def client(db: DatabaseManager):
    app = init_app(db)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_jobs_route_german_filter(db: DatabaseManager, client) -> None:
    # Setup test jobs
    db.insert_job(40001, "kw", "loc1")
    db.insert_job(40002, "kw", "loc2")
    db.update_job_details(40001, {"title": "Python Eng", "company_name": "Co 1"})
    db.update_job_details(40002, {"title": "Data Eng", "company_name": "Co 2"})
    db.save_screening_result(40001, ScreeningResult(0.9, "none", True, "Reason"))
    db.save_screening_result(40002, ScreeningResult(0.8, "high", True, "Reason"))

    # Selected jobs filtered by German=none
    response = client.get("/jobs?german=none")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Python Eng" in html
    assert "Data Eng" not in html

    # Selected jobs filtered by German=high
    response = client.get("/jobs?german=high")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Data Eng" in html
    assert "Python Eng" not in html

    # All jobs filtered by German=max_low
    response = client.get("/jobs/all?german=max_low")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Python Eng" in html
    assert "Data Eng" not in html
