"""Tests for the on-demand "Get Details" action on the job page."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from job_search.core.config import Config
from job_search.core.database import DatabaseManager
from job_search.scraping.details import JobNotFoundError, fetch_job_details
from job_search.web.app import init_app

JOB_ID = 4464637908


@pytest.fixture()
def client(db: DatabaseManager):
    app = init_app(
        db,
        config=Config.model_validate({
            "search": {
                "keywords": ["AI Engineer"],
                "locations": [{"geo_id": "1", "name": "Frankfurt"}],
                "blocked_companies": ["Blocked Corp"],
            },
        }),
    )
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture()
def prefiltered_job(db: DatabaseManager) -> int:
    """A job rejected by the title prefilter before its details were scraped."""
    db.insert_job(JOB_ID, "kw", "loc", title="Business Development Representative")
    db.mark_prefiltered(JOB_ID, "title:no required keyword")
    return JOB_ID


def _prefilter_reason(db: DatabaseManager) -> str | None:
    with db._cursor() as cur:
        cur.execute("SELECT prefilter_reason FROM jobs WHERE job_id = ?", (JOB_ID,))
        return cur.fetchone()[0]


def _post(client):
    return client.post(f"/jobs/{JOB_ID}/fetch-details", data={"source": "detail"}, follow_redirects=True)


def test_page_offers_get_details_instead_of_screen(client, prefiltered_job) -> None:
    html = client.get(f"/jobs/{JOB_ID}").get_data(as_text=True)

    assert f"/jobs/{JOB_ID}/fetch-details" in html
    assert f'action="/jobs/{JOB_ID}/screen"' not in html


def test_fetch_saves_details_and_keeps_prefilter_reason(client, db: DatabaseManager, prefiltered_job) -> None:
    fields = {"description": "Build AI systems.", "company_name": "Acme", "detected_language": "en"}
    with patch("job_search.core.session_store.load_session", return_value=MagicMock()), \
         patch("job_search.scraping.details.fetch_job_details", return_value=fields):
        html = _post(client).get_data(as_text=True)

    job = db.get_job_details(JOB_ID)
    assert job.description == "Build AI systems."
    assert job.scraped == 1
    assert _prefilter_reason(db) == "title:no required keyword"
    assert "Details fetched" in html
    # Once details exist, the page offers screening.
    assert f'action="/jobs/{JOB_ID}/screen"' in html


def test_blocked_company_is_saved_with_a_warning(client, db: DatabaseManager, prefiltered_job) -> None:
    fields = {"description": "Sales role.", "company_name": "Blocked Corp"}
    with patch("job_search.core.session_store.load_session", return_value=MagicMock()), \
         patch("job_search.scraping.details.fetch_job_details", return_value=fields):
        html = _post(client).get_data(as_text=True)

    assert db.job_exists(JOB_ID)
    assert "blocked companies list" in html


def test_missing_session_asks_for_login(client, db: DatabaseManager, prefiltered_job) -> None:
    with patch("job_search.core.session_store.load_session", return_value=None):
        html = _post(client).get_data(as_text=True)

    assert "job-search login" in html
    assert db.get_job_details(JOB_ID).description is None


def test_job_gone_from_linkedin_is_kept(client, db: DatabaseManager, prefiltered_job) -> None:
    with patch("job_search.core.session_store.load_session", return_value=MagicMock()), \
         patch("job_search.scraping.details.fetch_job_details", side_effect=JobNotFoundError(JOB_ID)):
        html = _post(client).get_data(as_text=True)

    assert db.job_exists(JOB_ID)
    assert "no longer exists on LinkedIn" in html


def test_expired_session_hint(client, prefiltered_job) -> None:
    with patch("job_search.core.session_store.load_session", return_value=MagicMock()), \
         patch("job_search.scraping.details.fetch_job_details",
               side_effect=RuntimeError(f"HTTP 401 for job {JOB_ID}: unauthorized")):
        html = _post(client).get_data(as_text=True)

    assert "session may have expired" in html


def test_fetch_job_details_raises_on_404() -> None:
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=404)

    with pytest.raises(JobNotFoundError):
        fetch_job_details(session, JOB_ID, headers={})
