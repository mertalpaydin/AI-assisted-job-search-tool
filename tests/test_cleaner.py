from __future__ import annotations

from unittest.mock import MagicMock
import pytest
import requests

from job_search.cleaner.cleaner import JobCleaner
from job_search.core.database import DatabaseManager


def test_is_job_expired_closed_html(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    cleaner = JobCleaner(db)

    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption>'
    mock_session.get.return_value = mock_resp

    cleaner._session = mock_session
    assert cleaner.is_job_expired(4423912764) is True


def test_is_job_expired_301_redirect(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    cleaner = JobCleaner(db)

    mock_session = MagicMock(spec=requests.Session)
    mock_guest_resp = MagicMock()
    mock_guest_resp.status_code = 500  # API fallback

    mock_view_resp = MagicMock()
    mock_view_resp.status_code = 301
    mock_view_resp.headers = {"Location": "https://de.linkedin.com/jobs/analyst-stellen?trk=expired_jd_redirect"}

    mock_session.get.side_effect = [mock_guest_resp, mock_view_resp]
    cleaner._session = mock_session

    assert cleaner.is_job_expired(4423912764) is True


def test_clean_pending_jobs(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))

    # Insert a job
    db.insert_job(1234567, keyword="test", location_id="test")

    cleaner = JobCleaner(db)
    cleaner.is_job_expired = MagicMock(return_value=True)

    result = cleaner.clean_pending_jobs(limit=10, delay_between=0)
    assert result["checked"] == 1
    assert result["expired"] == 1
    assert result["expired_ids"] == [1234567]

    # Verify status in DB
    job = db.get_selected_job(1234567)
    assert job is not None
    assert job.application_status == "expired"
