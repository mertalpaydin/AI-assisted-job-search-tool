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
