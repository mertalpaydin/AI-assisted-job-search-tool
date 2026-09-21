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


@pytest.fixture()
def configured_client(db: DatabaseManager):
    """A client whose app has a Config, which the generate routes require."""
    from job_search.core.config import Config

    app = init_app(db, config=Config.model_validate({
        "search": {
            "keywords": ["AI Engineer"],
            "locations": [{"geo_id": "1", "name": "Frankfurt"}],
        },
    }))
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


def test_clean_cover_letter_text() -> None:
    from job_search.utils.formatting import clean_cover_letter_text

    raw = "Dear Manager,\n\n\nI am writing to apply.\n\nThank you.\n\n"
    expected = "Dear Manager,\nI am writing to apply.\nThank you."
    assert clean_cover_letter_text(raw) == expected
    assert clean_cover_letter_text(None) == ""


def test_cover_letter_update_and_rendering(db: DatabaseManager, client) -> None:
    db.insert_job(50001, "kw", "loc")
    db.update_job_details(50001, {"title": "AI Engineer", "company_name": "AI Co"})
    db.save_screening_result(50001, ScreeningResult(0.95, "none", True, "Great candidate"))
    
    # Post cover letter text with paragraph breaks
    raw_cl = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    res = client.post(
        "/jobs/50001/cover-letter/update",
        data={"cover_letter_text": raw_cl},
        follow_redirects=True,
    )
    assert res.status_code == 200

    # Verify database retains original paragraph breaks for Web UI display
    job = db.get_selected_job(50001)
    assert job is not None
    assert job.cover_letter_text == raw_cl

    # Verify detail HTML includes non-scrollable textarea, JS formatting logic, and Generate PDF button
    html = res.get_data(as_text=True)
    assert "overflow: hidden" in html
    assert "textarea.value.replace(/\\r\\n/g, \"\\n\").replace(/\\n\\s*\\n+/g, \"\\n\")" in html
    assert "Paragraph 1\n\nParagraph 2\n\nParagraph 3" in html
    assert "Generate PDF" in html


def test_get_a_or_an_article() -> None:
    from job_search.export.latex_exporter import get_a_or_an
    assert get_a_or_an("AI Engineer") == "an"
    assert get_a_or_an("ERP Specialist") == "an"
    assert get_a_or_an("Data Scientist") == "a"
    assert get_a_or_an("ML Engineer") == "an"
    assert get_a_or_an("Procurement Manager") == "a"


def test_cover_letter_pdf_route(db: DatabaseManager, client, tmp_path) -> None:
    from unittest.mock import patch
    db.insert_job(60001, "kw", "loc")
    db.update_job_details(60001, {"title": "Data Engineer", "company_name": "Tech Corp"})
    db.save_screening_result(60001, ScreeningResult(0.9, "none", True, "Reason"))
    db.save_cover_letter(60001, "Dear Hiring Manager,\n\nI am writing to express my interest in the role.\n\nSincerely,\nTest Applicant", "model", 0)

    # Use isolated tmp_path for test PDF output so real export directories are not affected
    dummy_pdf = tmp_path / "Applicant_Name_CoverLetter_Tech_Corp.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 header test content " + b"0" * 1000)

    with patch("job_search.export.latex_exporter.generate_cover_letter_pdf", return_value=dummy_pdf):
        # Test direct GET download
        res = client.get("/jobs/60001/cover-letter/pdf")
        assert res.status_code == 200
        assert res.mimetype == "application/pdf"
        assert len(res.data) > 1000

        # Test POST with live unsaved overrides returning JSON status
        post_res = client.post(
            "/jobs/60001/cover-letter/pdf",
            data={
                "job_title": "AI Architect",
                "company_name": "Live Corp",
                "cover_letter_text": "Dear Hiring Manager,\n\nLive unsaved edit text.\n\nSincerely,\nTest Applicant",
            },
        )
        assert post_res.status_code == 200
        json_data = post_res.get_json()
        assert json_data["success"] is True


def test_cover_letter_short_and_long_generation(db: DatabaseManager, tmp_path) -> None:
    from pypdf import PdfReader
    from job_search.export.latex_exporter import generate_cover_letter_pdf

    db.insert_job(70001, "kw", "loc")
    db.update_job_details(70001, {"title": "AI Engineer", "company_name": "Test Co"})
    db.save_screening_result(70001, ScreeningResult(0.9, "none", True, "Reason"))

    # 1. Short cover letter
    short_text = "Dear Hiring Manager,\n\nShort cover letter test body.\n\nSincerely,\nApplicant Name"
    short_pdf = generate_cover_letter_pdf(
        70001, db, ".", output_pdf_path=tmp_path / "short.pdf", override_cl_text=short_text
    )
    assert short_pdf.exists()
    assert len(PdfReader(short_pdf).pages) == 1

    # 2. Long cover letter
    long_text = "Dear Hiring Manager,\n\n" + ("Paragraph text detailing machine learning projects and AI pipeline architectures. " * 8 + "\n\n") * 3 + "Sincerely,\nApplicant Name"
    long_pdf = generate_cover_letter_pdf(
        70001, db, ".", output_pdf_path=tmp_path / "long.pdf", override_cl_text=long_text
    )
    assert long_pdf.exists()
    assert len(PdfReader(long_pdf).pages) == 1

def test_quick_action_redirect_preserves_referrer_filters(db: DatabaseManager, client) -> None:
    db.insert_job(80001, "kw", "loc")
    db.update_job_details(80001, {"title": "Dev Ops", "company_name": "Cloud Co", "applyMethod": '{"easyApplyUrl": "http://example.com"}'})
    db.save_screening_result(80001, ScreeningResult(0.9, "none", True, "Pass"))

    target_referrer = "/jobs?page=1&sort=created_at&dir=desc&status=pending&min_match=0.85&apply_type=easy"
    
    # POST quick-apply sending Referer header
    res = client.post(
        "/jobs/80001/quick-apply",
        data={"status_filter": "pending"},
        headers={"Referer": target_referrer},
    )
    assert res.status_code == 302
    assert res.headers["Location"] == target_referrer


def test_company_inclusion_filter_web(db: DatabaseManager, client) -> None:
    db.insert_job(90001, "kw", "loc1")
    db.insert_job(90002, "kw", "loc2")
    db.update_job_details(90001, {"title": "Frontend Dev", "company_name": "Web Corp"})
    db.update_job_details(90002, {"title": "Backend Dev", "company_name": "Server Corp"})
    db.save_screening_result(90001, ScreeningResult(0.9, "none", True, "Pass"))
    db.save_screening_result(90002, ScreeningResult(0.85, "none", True, "Pass"))

    # Test inc parameter in web route
    res = client.get("/jobs?inc=Web+Corp")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Frontend Dev" in html
    assert "Backend Dev" not in html
    assert "1 selected" in html


def test_cover_letter_delete_route(db: DatabaseManager, client) -> None:
    db.insert_job(95001, "kw", "loc")
    db.update_job_details(95001, {"title": "Fullstack Dev", "company_name": "Tech Corp"})
    db.save_screening_result(95001, ScreeningResult(0.9, "none", True, "Pass"))
    db.save_cover_letter(95001, "Original Cover Letter", "gemini-3-flash", 0)

    # Verify cover letter exists before delete
    assert db.has_successful_cover_letter(95001)

    # Post to delete cover letter
    res = client.post("/jobs/95001/cover-letter/delete", follow_redirects=True)
    assert res.status_code == 200

    # Verify DB state: cover letter row removed, user_cl_approved=0, application_status='skipped'
    assert not db.has_successful_cover_letter(95001)
    job = db.get_selected_job(95001)
    assert job is not None
    assert job.user_cl_approved == 0
    assert job.application_status == "skipped"


def test_cover_letter_regenerate_route(db: DatabaseManager, client) -> None:
    db.insert_job(95002, "kw", "loc")
    db.update_job_details(95002, {"title": "ML Engineer", "company_name": "AI Corp"})
    db.save_screening_result(95002, ScreeningResult(0.95, "none", True, "Pass"))
    db.save_cover_letter(95002, "Old Cover Letter", "gemini-3-flash", 0)

    # Post to regenerate cover letter
    res = client.post("/jobs/95002/cover-letter/regenerate", follow_redirects=True)
    assert res.status_code == 200

    # Verify DB state: cover letter row removed, user_cl_approved=1, job is in get_jobs_pending_cover_letter
    assert not db.has_successful_cover_letter(95002)
    job = db.get_selected_job(95002)
    assert job is not None
    assert job.user_cl_approved == 1
    pending = db.get_jobs_pending_cover_letter(mode="user_approval")
    assert 95002 in pending




# ---------------------------------------------------------------------------
# Recruiter message
#
# The one route in this UI that calls Gemini inside the request, so the tests
# monkeypatch the generator and assert the plumbing around it: the length is
# validated, the result is persisted, and a failure comes back as JSON rather
# than a traceback.
# ---------------------------------------------------------------------------

def _seed_job(db: DatabaseManager, job_id: int) -> None:
    db.insert_job(job_id, "kw", "loc")
    db.update_job_details(job_id, {"title": "AI Engineer", "company_name": "Acme"})
    db.save_screening_result(job_id, ScreeningResult(0.9, "none", True, "Pass"))


def test_recruiter_message_generate_saves_and_returns_json(
    db: DatabaseManager, configured_client, monkeypatch
) -> None:
    _seed_job(db, 96001)

    from job_search.ai import recruiter_message as rm
    monkeypatch.setattr(
        rm, "generate_recruiter_message",
        lambda **kw: f"Hi, about the {kw['length']} role.",
    )

    res = configured_client.post(
        "/jobs/96001/recruiter-message/generate", data={"length": "inmail"}
    )
    assert res.status_code == 200
    payload = res.get_json()
    assert payload["success"] is True
    assert payload["kind"] == "inmail"
    assert payload["chars"] == len(payload["text"])
    assert payload["limit"] == 1500

    job = db.get_selected_job(96001)
    assert job.recruiter_message == payload["text"]
    assert job.recruiter_message_kind == "inmail"
    assert job.recruiter_message_at is not None


def test_recruiter_message_generate_rejects_unknown_length(
    db: DatabaseManager, configured_client
) -> None:
    _seed_job(db, 96002)
    res = configured_client.post(
        "/jobs/96002/recruiter-message/generate", data={"length": "telegram"}
    )
    assert res.status_code == 400
    assert "telegram" in res.get_json()["error"]
    assert db.get_selected_job(96002).recruiter_message is None


def test_recruiter_message_generate_reports_failure_as_json(
    db: DatabaseManager, configured_client, monkeypatch
) -> None:
    """A provider failure must not leave a half-written message behind."""
    _seed_job(db, 96003)

    from job_search.ai import recruiter_message as rm

    def _boom(**kw):
        raise rm.RecruiterMessageError("No Gemini API keys configured.")

    monkeypatch.setattr(rm, "generate_recruiter_message", _boom)

    res = configured_client.post(
        "/jobs/96003/recruiter-message/generate", data={"length": "note"}
    )
    assert res.status_code == 502
    assert "No Gemini API keys" in res.get_json()["error"]
    assert db.get_selected_job(96003).recruiter_message is None


def test_recruiter_message_update_and_clear(db: DatabaseManager, client) -> None:
    _seed_job(db, 96004)
    db.save_recruiter_message(96004, "Generated draft", kind="note")

    res = client.post(
        "/jobs/96004/recruiter-message/update",
        data={"recruiter_message": "  My edited draft  ", "source": "detail"},
        follow_redirects=True,
    )
    assert res.status_code == 200
    job = db.get_selected_job(96004)
    assert job.recruiter_message == "My edited draft"
    # An edit must not erase which form the text was generated as.
    assert job.recruiter_message_kind == "note"

    # Saving an empty box clears the message rather than storing "".
    client.post(
        "/jobs/96004/recruiter-message/update",
        data={"recruiter_message": "   ", "source": "detail"},
        follow_redirects=True,
    )
    assert db.get_selected_job(96004).recruiter_message is None


def test_recruiter_message_delete_route(db: DatabaseManager, client) -> None:
    _seed_job(db, 96005)
    db.save_recruiter_message(96005, "Draft to remove", kind="inmail")

    res = client.post("/jobs/96005/recruiter-message/delete",
                      data={"source": "detail"}, follow_redirects=True)
    assert res.status_code == 200
    job = db.get_selected_job(96005)
    assert job.recruiter_message is None
    assert job.recruiter_message_kind is None
    assert job.recruiter_message_at is None


def test_recruiter_message_routes_404_on_unknown_job(client) -> None:
    assert client.post("/jobs/99999/recruiter-message/update",
                       data={"recruiter_message": "x"}).status_code == 404
    assert client.post("/jobs/99999/recruiter-message/delete").status_code == 404


def test_job_detail_renders_the_recruiter_card(db: DatabaseManager, client) -> None:
    _seed_job(db, 96006)
    db.save_recruiter_message(96006, "Existing draft", kind="inmail")

    body = client.get("/jobs/96006").get_data(as_text=True)
    assert "Recruiter Message" in body
    assert "Existing draft" in body
    # The card must open on the length the message was generated as.
    assert '"inmail"' in body
    assert '"note": 300' in body or '"note":300' in body


def test_job_detail_renders_scrape_date(db: DatabaseManager, client) -> None:
    from job_search.web.app import long_date_filter
    _seed_job(db, 96007)
    res = client.get("/jobs/96007")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "Scrape Date" in body
    job = db.get_selected_job(96007)
    assert job is not None and job.created_at is not None
    assert long_date_filter(job.created_at) in body
    # Verify format like 'September 12, 2026'
    assert long_date_filter("2026-09-12 14:30:00") == "September 12, 2026"
