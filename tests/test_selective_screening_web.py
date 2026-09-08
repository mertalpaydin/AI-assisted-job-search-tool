"""Tests for selective screening web UI routes, filters, and on-demand actions."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from job_search.core.config import Config
from job_search.core.database import DatabaseManager, ScreeningResult
from job_search.web.app import init_app


@pytest.fixture()
def client(db: DatabaseManager):
    app = init_app(db)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture()
def configured_client(db: DatabaseManager):
    app = init_app(
        db,
        config=Config.model_validate({
            "search": {
                "keywords": ["AI Engineer"],
                "locations": [{"geo_id": "1", "name": "Frankfurt"}],
            },
        }),
    )
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_jobs_all_screened_and_language_filters(db: DatabaseManager, client) -> None:
    # Job 1: English + Screened
    db.insert_job(50001, "kw", "loc1")
    db.update_job_details(50001, {
        "title": "English Screened Eng",
        "company_name": "BigCorp",
        "scraped": 1,
        "description": "English desc",
        "detected_language": "en",
        "german_stopword_ratio": 0.05,
    })
    db.save_screening_result(50001, ScreeningResult(0.85, "none", True, "Great"))

    # Job 2: German + Unscreened
    db.insert_job(50002, "kw", "loc2")
    db.update_job_details(50002, {
        "title": "German Unscreened Eng",
        "company_name": "DE GmbH",
        "scraped": 1,
        "description": "German beschreibung",
        "detected_language": "de",
        "german_stopword_ratio": 0.88,
    })

    # Job 3: English + Unscreened
    db.insert_job(50003, "kw", "loc3")
    db.update_job_details(50003, {
        "title": "English Unscreened Eng",
        "company_name": "Startup Inc",
        "scraped": 1,
        "description": "English startup desc",
        "detected_language": "en",
        "german_stopword_ratio": 0.02,
    })

    # 1. Filter screened=unscreened -> Jobs 2 and 3, not Job 1
    res = client.get("/jobs/all?screened=unscreened")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "German Unscreened Eng" in html
    assert "English Unscreened Eng" in html
    assert "English Screened Eng" not in html

    # 2. Filter screened=screened -> Job 1, not Jobs 2 and 3
    res = client.get("/jobs/all?screened=screened")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "English Screened Eng" in html
    assert "German Unscreened Eng" not in html
    assert "English Unscreened Eng" not in html

    # 3. Filter lang=de -> Job 2
    res = client.get("/jobs/all?lang=de")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "German Unscreened Eng" in html
    assert "English Screened Eng" not in html
    assert "English Unscreened Eng" not in html

    # 4. Filter lang=en -> Jobs 1 and 3
    res = client.get("/jobs/all?lang=en")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "English Screened Eng" in html
    assert "English Unscreened Eng" in html
    assert "German Unscreened Eng" not in html

    # 5. Combined: screened=unscreened&lang=de -> only Job 2
    res = client.get("/jobs/all?screened=unscreened&lang=de")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "German Unscreened Eng" in html
    assert "English Unscreened Eng" not in html

    # 6. Selected jobs route /jobs?lang=...
    db.save_screening_result(50002, ScreeningResult(0.80, "high", True, "German ok"))

    # /jobs?lang=en -> Job 1, not Job 2
    res_sel_en = client.get("/jobs?lang=en")
    assert res_sel_en.status_code == 200
    html_sel_en = res_sel_en.get_data(as_text=True)
    assert "English Screened Eng" in html_sel_en
    assert "German Unscreened Eng" not in html_sel_en

    # /jobs?lang=de -> Job 2, not Job 1
    res_sel_de = client.get("/jobs?lang=de")
    assert res_sel_de.status_code == 200
    html_sel_de = res_sel_de.get_data(as_text=True)
    assert "German Unscreened Eng" in html_sel_de
    assert "English Screened Eng" not in html_sel_de


def test_job_detail_unscreened_renders_banner_and_button(db: DatabaseManager, client) -> None:
    db.insert_job(50004, "kw", "loc")
    db.update_job_details(50004, {
        "title": "Deferred Data Scientist",
        "company_name": "Micro Co",
        "scraped": 1,
        "description": "Full job description text",
        "detected_language": "de",
        "german_stopword_ratio": 0.85,
        "company_staff_count": 8,
    })

    res = client.get("/jobs/50004")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Unscreened Job (Deferred from Auto-Screening)" in html
    assert "German Ad" in html
    assert "Screen Job Now" in html
    assert "/jobs/50004/screen" in html


def test_screen_single_job_route(db: DatabaseManager, configured_client) -> None:
    db.insert_job(50005, "kw", "loc")
    db.update_job_details(50005, {
        "title": "On Demand Job",
        "company_name": "Test Co",
        "scraped": 1,
        "description": "Valid job description",
    })

    fake_result = ScreeningResult(
        cv_match_score=0.92,
        german_requirement_level="none",
        is_selected=True,
        reasoning="Good match",
    )

    with patch("job_search.ai.screener.screen_single_job", return_value=fake_result) as mock_screen:
        res = configured_client.post(
            "/jobs/50005/screen",
            data={"source": "detail"},
            follow_redirects=True,
        )
        assert res.status_code == 200
        mock_screen.assert_called_once()
        html = res.get_data(as_text=True)
        assert "Job #50005 screened successfully: SELECTED (Match: 92%)" in html

    # 404 on non-existent job
    assert configured_client.post("/jobs/99999/screen").status_code == 404


def test_batch_screen_jobs_route(db: DatabaseManager, configured_client) -> None:
    db.insert_job(50006, "kw", "loc")
    db.insert_job(50007, "kw", "loc")
    db.update_job_details(50006, {"title": "Batch Job 1", "scraped": 1, "description": "Desc 1"})
    db.update_job_details(50007, {"title": "Batch Job 2", "scraped": 1, "description": "Desc 2"})

    mock_secrets = MagicMock()
    mock_secrets.gemini_api_keys = ["mock-key-123"]

    with patch("job_search.core.config.load_secrets", return_value=mock_secrets), \
         patch("job_search.ai.batch_screener.BatchScreener.submit", return_value=789) as mock_submit:
        res = configured_client.post(
            "/jobs/batch-screen",
            data={"job_ids": ["50006", "50007"], "show_all": "1"},
            follow_redirects=True,
        )
        assert res.status_code == 200
        mock_submit.assert_called_once_with([50006, 50007])
        html = res.get_data(as_text=True)
        assert "Submitted 2 job(s) for batch screening (Batch #789)" in html


def test_runner_status_stats(db: DatabaseManager, configured_client) -> None:
    # Seed 1 auto-eligible job (mid-size, english)
    db.insert_job(50008, "kw", "loc")
    db.update_job_details(50008, {
        "title": "Auto Job",
        "company_name": "MidCorp",
        "scraped": 1,
        "description": "Desc",
        "company_staff_count": 500,
        "detected_language": "en",
        "german_stopword_ratio": 0.05,
    })

    # Seed 1 deferred job (german)
    db.insert_job(50009, "kw", "loc")
    db.update_job_details(50009, {
        "title": "Deferred Job",
        "company_name": "GermanCorp",
        "scraped": 1,
        "description": "Beschreibung",
        "company_staff_count": 500,
        "detected_language": "de",
        "german_stopword_ratio": 0.90,
    })

    res = configured_client.get("/runner/status")
    assert res.status_code == 200
    data = res.get_json()
    stats = data["pipeline_stats"]
    assert stats["screen_pending"] == 2
    assert stats["screen_pending_auto"] == 1
    assert stats["screen_deferred"] == 1


def test_unscreened_jobs_route(db: DatabaseManager, client) -> None:
    # 1. Unscreened normal job
    db.insert_job(60001, "kw", "loc1")
    db.update_job_details(60001, {
        "title": "Pending Unscreened Dev",
        "company_name": "Tech Corp",
        "scraped": 1,
        "description": "Dev job in English",
        "detected_language": "en",
    })

    # 2. Screened job
    db.insert_job(60002, "kw", "loc2")
    db.update_job_details(60002, {
        "title": "Already Screened Dev",
        "company_name": "Tech Corp",
        "scraped": 1,
        "description": "Dev job",
    })
    db.save_screening_result(60002, ScreeningResult(0.9, "none", True, "Good"))

    # 3. Prefiltered job (should NOT show in unscreened)
    db.insert_job(60003, "kw", "loc3", prefilter_reason="excluded_keyword", title="Prefiltered Dev")
    db.update_job_details(60003, {
        "title": "Prefiltered Dev",
        "company_name": "Tech Corp",
        "scraped": 1,
        "description": "Dev job",
    })

    # GET /jobs/unscreened
    res = client.get("/jobs/unscreened")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Unscreened Jobs" in html
    assert "Pending Unscreened Dev" in html
    assert "Already Screened Dev" not in html
    assert "Prefiltered Dev" not in html
    # Check left sidebar contains Unscreened Jobs link
    assert 'href="/jobs/unscreened"' in html

    # Verify job detail "Screen Job Now" button doesn't have broken quotes/text
    detail_res = client.get("/jobs/60001")
    assert detail_res.status_code == 200
    detail_html = detail_res.get_data(as_text=True)
    assert "Screen Job Now" in detail_html
    assert "Screening...&#39;; this.form.submit;" not in detail_html
    assert "Screening...'; this.form.submit;" not in detail_html


def test_unscreened_batch_action_redirect(configured_client) -> None:
    mock_secrets = MagicMock()
    mock_secrets.gemini_api_keys = ["mock-key-123"]

    with patch("job_search.core.config.load_secrets", return_value=mock_secrets), \
         patch("job_search.ai.batch_screener.BatchScreener.submit", return_value=999) as mock_submit:
        res = configured_client.post(
            "/jobs/batch-screen",
            data={"job_ids": ["60001"], "is_unscreened": "1"},
            follow_redirects=False,
        )
        assert res.status_code == 302
        assert "/jobs/unscreened" in res.headers["Location"]
        mock_submit.assert_called_once_with([60001])


