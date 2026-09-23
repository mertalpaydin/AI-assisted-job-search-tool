"""
Tests for External Job Search module:
- ExternalDatabaseManager (CRUD, filtering, multi-source, language detection, date range, stats)
- LinkedInMatcher (local jobs.db fuzzy/exact matching & status extraction)
- Title & Blocked Company Prefiltering in ExternalSearchOrchestrator
- Provider Quota and Error Resilience
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from job_search.core.config import load_config
from job_search.core.external_database import ExternalDatabaseManager
from job_search.scraping.external.matcher import LinkedInMatcher
from job_search.scraping.external.orchestrator import ExternalSearchOrchestrator
from job_search.scraping.external.providers import BaseProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ext_db(tmp_path: Path) -> ExternalDatabaseManager:
    db_path = tmp_path / "test_external_jobs.db"
    return ExternalDatabaseManager(db_path=db_path)


@pytest.fixture
def linkedin_db(tmp_path: Path) -> Path:
    """Create a minimal mock LinkedIn jobs.db database with real schema rows."""
    db_path = tmp_path / "mock_linkedin_jobs.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE jobs (
            job_id INTEGER PRIMARY KEY,
            title TEXT,
            company_name TEXT,
            location TEXT,
            application_status TEXT DEFAULT 'pending',
            is_selected INTEGER DEFAULT 0,
            cv_match_score REAL DEFAULT NULL
        )
    """)
    conn.execute("""
        INSERT INTO jobs (job_id, title, company_name, location, application_status, is_selected, cv_match_score)
        VALUES 
            (101, 'AI Solutions Architect', 'Cisco Systems', 'Frankfurt', 'applied', 1, 0.88),
            (102, 'Machine Learning Engineer', 'SAP SE', 'Walldorf', 'pending', 1, 0.92),
            (103, 'Junior Data Analyst', 'Siemens AG', 'Munich', 'skipped', 0, 0.40)
    """)
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. ExternalDatabaseManager Tests
# ---------------------------------------------------------------------------

def test_external_db_upsert_and_retrieve(ext_db: ExternalDatabaseManager):
    job_data = {
        "source": "indeed",
        "external_id": "ind-12345",
        "title": "Senior AI Engineer",
        "company_name": "Tech Corp",
        "location": "Frankfurt, Germany",
        "url": "https://de.indeed.com/viewjob?jk=ind-12345",
        "description": "We are seeking a senior AI engineer with deep knowledge of Python, machine learning, and cloud architecture to lead our team.",
        "prefilter_status": "accepted",
        "application_status": "new",
    }

    # 1. Insert new job
    job_id, is_new = ext_db.upsert_job(job_data)
    assert is_new is True
    assert isinstance(job_id, int)
    assert job_id > 0

    # 2. Fetch job by ID
    job = ext_db.get_job(job_id)
    assert job is not None
    assert job["title"] == "Senior AI Engineer"
    assert job["company_name"] == "Tech Corp"
    assert job["source"] == "indeed"
    # Lingo normalization: 'new' is normalized to 'pending'
    assert job["application_status"] == "pending"
    # Auto language detection for English description
    assert job["detected_language"] == "en"

    # 3. Update existing job (same source + external_id)
    updated_data = dict(job_data)
    updated_data["title"] = "Lead AI Engineer"
    same_job_id, is_new2 = ext_db.upsert_job(updated_data)
    assert is_new2 is False
    assert same_job_id == job_id

    refetched = ext_db.get_job(job_id)
    assert refetched["title"] == "Lead AI Engineer"


def test_external_db_filtering_and_stats(ext_db: ExternalDatabaseManager):
    jobs = [
        {
            "source": "indeed", "external_id": "1", "title": "AI Specialist", "company_name": "Alpha",
            "application_status": "pending", "prefilter_status": "accepted",
            "description": "This is an English job posting for an experienced AI Specialist."
        },
        {
            "source": "arbeitsagentur", "external_id": "2", "title": "Data Scientist", "company_name": "Beta",
            "application_status": "applied", "prefilter_status": "accepted",
            "description": "Wir suchen für unseren Standort einen Data Scientist für maschinelles Lernen und statistische Datenanalyse."
        },
        {
            "source": "serpapi", "external_id": "3", "title": "ML Engineer", "company_name": "Gamma",
            "application_status": "skipped", "prefilter_status": "accepted",
            "description": "We are looking for an ML Engineer to develop state of the art models."
        },
        {
            "source": "rapidapi", "external_id": "4", "title": "Senior Accountant", "company_name": "Delta",
            "application_status": "pending", "prefilter_status": "filtered_title",
            "description": "General accounting and financial auditing position."
        },
    ]
    for j in jobs:
        ext_db.upsert_job(j)

    # 1. Multi-source filter (indeed + serpapi)
    multi_jobs, count = ext_db.get_jobs(source=["indeed", "serpapi"])
    assert count == 2
    sources = {j["source"] for j in multi_jobs}
    assert sources == {"indeed", "serpapi"}

    # 2. Filter by status: applied
    applied_jobs, count = ext_db.get_jobs(application_status="applied")
    assert count == 1
    assert applied_jobs[0]["company_name"] == "Beta"

    # 3. Filter by status: skipped
    skipped_jobs, count = ext_db.get_jobs(application_status="skipped")
    assert count == 1
    assert skipped_jobs[0]["company_name"] == "Gamma"

    # 4. Filter by language: German
    de_jobs, count = ext_db.get_jobs(language="de")
    assert count == 1
    assert de_jobs[0]["company_name"] == "Beta"

    # 5. Free text search
    search_res, count = ext_db.get_jobs(search_query="Alpha")
    assert count == 1
    assert search_res[0]["company_name"] == "Alpha"

    # 6. Stats validation
    stats = ext_db.get_stats()
    assert stats["total_accepted"] == 3
    assert stats["total_prefiltered"] == 1
    assert stats["by_status"]["pending"] == 1
    assert stats["by_status"]["applied"] == 1
    assert stats["by_status"]["skipped"] == 1
    assert stats["by_source"]["indeed"] == 1
    assert stats["by_source"]["arbeitsagentur"] == 1
    assert stats["by_language"]["de"] == 1
    assert stats["by_language"]["en"] == 2

    # 7. Test matched LinkedIn status filtering
    ext_db.upsert_job({
        "source": "indeed", "external_id": "matched-1", "title": "AI Lead", "company_name": "Epsilon",
        "matched_linkedin_job_id": 101, "matched_linkedin_status": "LinkedIn: Applied",
        "prefilter_status": "accepted", "description": "English lead position."
    })
    ext_db.upsert_job({
        "source": "indeed", "external_id": "matched-2", "title": "AI Director", "company_name": "Zeta",
        "matched_linkedin_job_id": 102, "matched_linkedin_status": "LinkedIn: Selected (0.88)",
        "prefilter_status": "accepted", "description": "English director position."
    })

    # Test hide_applied
    hide_applied_jobs, count_hide = ext_db.get_jobs(matched_only="hide_applied")
    applied_on_li = [j for j in hide_applied_jobs if j.get("matched_linkedin_status") == "LinkedIn: Applied"]
    assert len(applied_on_li) == 0

    # Test applied filter
    applied_jobs, count_applied = ext_db.get_jobs(matched_only="applied")
    assert count_applied == 1
    assert applied_jobs[0]["company_name"] == "Epsilon"

    # Test selected filter
    sel_jobs, count_sel = ext_db.get_jobs(matched_only="selected")
    assert count_sel == 1
    assert sel_jobs[0]["company_name"] == "Zeta"

    # Test multi-status matched filter (net_new + selected)
    # Total accepted so far: 3 original (net_new) + 1 matched-1 (applied) + 1 matched-2 (selected) = 5
    # net_new (3) + selected (1) = 4
    multi_jobs, count_multi = ext_db.get_jobs(matched_only=["net_new", "selected"])
    assert count_multi == 4
    multi_companies = {j["company_name"] for j in multi_jobs}
    assert "Epsilon" not in multi_companies  # Epsilon is applied
    assert "Zeta" in multi_companies
    assert "Alpha" in multi_companies

    # Verify by_linkedin_status in stats
    stats_updated = ext_db.get_stats()
    assert stats_updated["by_linkedin_status"]["applied"] == 1
    assert stats_updated["by_linkedin_status"]["selected"] == 1
    assert stats_updated["by_linkedin_status"]["net_new"] == 3
    assert stats_updated["by_linkedin_status"]["hide_applied"] == 4


def test_external_db_cover_letter_and_status_buttons(ext_db: ExternalDatabaseManager):
    job_id, _ = ext_db.upsert_job({
        "source": "indeed",
        "external_id": "test-cl-1",
        "title": "AI Consultant",
        "company_name": "Consulting Ltd",
        "application_status": "pending",
    })

    # Update to applied
    ext_db.update_application_status(job_id, status="applied")
    job = ext_db.get_job(job_id)
    assert job["application_status"] == "applied"
    assert job["applied_at"] is not None

    # Update to skipped (using button)
    ext_db.update_application_status(job_id, status="skipped")
    job_skipped = ext_db.get_job(job_id)
    assert job_skipped["application_status"] == "skipped"

    # Clear status (reset to pending)
    ext_db.update_application_status(job_id, status="")
    job_reset = ext_db.get_job(job_id)
    assert job_reset["application_status"] == "pending"

    # Save cover letter
    cl_text = "Dear Hiring Manager, I am excited to apply..."
    ext_db.update_cover_letter(job_id, cl_text=cl_text)
    job_after_cl = ext_db.get_job(job_id)
    assert job_after_cl["cover_letter_text"] == cl_text


# ---------------------------------------------------------------------------
# 2. LinkedInMatcher Tests
# ---------------------------------------------------------------------------

def test_linkedin_matcher_exact_and_fuzzy(linkedin_db: Path):
    matcher = LinkedInMatcher(db_path=linkedin_db)

    # 1. Exact / High fuzzy match: "Cisco Systems" vs "Cisco", "AI Solutions Architect"
    res1 = matcher.find_match(company_name="Cisco", title="AI Solutions Architect (m/f/d)")
    assert res1 is not None
    assert res1["matched_linkedin_job_id"] == 101
    assert "Applied" in res1["matched_linkedin_status"]
    assert res1["matched_similarity"] >= 0.70

    # 2. Matching SAP SE with exact title
    res2 = matcher.find_match(company_name="SAP", title="Machine Learning Engineer")
    assert res2 is not None
    assert res2["matched_linkedin_job_id"] == 102
    assert "Selected" in res2["matched_linkedin_status"]

    # 3. Non-match
    res3 = matcher.find_match(company_name="Completely Unknown GmbH", title="Quantum Specialist")
    assert res3 is None


# ---------------------------------------------------------------------------
# 3. Orchestrator Prefiltering & Provider Error Resilience
# ---------------------------------------------------------------------------

class DummyWorkingProvider(BaseProvider):
    name = "dummy_working"

    def search(self, keyword: str, location: str, limit: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "source": "dummy_working",
                "external_id": "dummy-1",
                "title": "AI Innovation Lead",
                "company_name": "Valid Innovations Corp",
                "location": location,
                "url": "https://example.com/job1",
                "description": "AI engineering role with Python.",
            },
            {
                "source": "dummy_working",
                "external_id": "dummy-2",
                "title": "Senior Werkstudent Office Assistant",  # Negatively prefiltered title
                "company_name": "Random Corp",
                "location": location,
                "url": "https://example.com/job2",
                "description": "Office assistance work.",
            },
            {
                "source": "dummy_working",
                "external_id": "dummy-3",
                "title": "Lead AI Architect",
                "company_name": "Blocked Company Ltd",  # Blocked company
                "location": location,
                "url": "https://example.com/job3",
                "description": "Architecture work.",
            },
        ]


class DummyFailingProvider(BaseProvider):
    name = "dummy_failing"

    def search(self, keyword: str, location: str, limit: int = 10) -> list[dict[str, Any]]:
        raise RuntimeError("API Rate Limit exceeded (429 Too Many Requests)")


def test_orchestrator_prefiltering_and_fault_tolerance(tmp_path: Path):
    ext_db = ExternalDatabaseManager(db_path=tmp_path / "test_ext_orch.db")
    mock_li_db = tmp_path / "empty_li.db"
    conn = sqlite3.connect(str(mock_li_db))
    conn.execute("CREATE TABLE jobs (job_id INTEGER PRIMARY KEY, title TEXT, company_name TEXT, application_status TEXT, is_selected INTEGER, cv_match_score REAL)")
    conn.commit()
    conn.close()

    # Create dummy config with blocked companies
    config = load_config("config/config.yaml")
    orchestrator = ExternalSearchOrchestrator(
        config=config,
        external_db=ext_db,
    )
    # Explicitly configure blocked companies & matcher
    orchestrator.blocked_companies = frozenset(["blocked company ltd"])
    orchestrator.matcher = LinkedInMatcher(db_path=mock_li_db)

    # Plug in dummy providers
    working = DummyWorkingProvider()
    failing = DummyFailingProvider()
    orchestrator.providers = {
        "dummy_working": working,
        "dummy_failing": failing,
    }

    # Run search across both
    stats = orchestrator.run_search(
        provider_names=["dummy_working", "dummy_failing"],
        keywords_override=["AI Innovation"],
        location_override="Frankfurt",
        limit_per_search=5,
    )

    # Verify that the failing provider did not stop the run
    assert stats["total_found"] == 3
    assert stats["prefiltered_company"] == 1
    assert stats["prefiltered_title"] >= 1

    # Check database records (query all prefilter statuses)
    jobs, count = ext_db.get_jobs(prefilter_status=None)
    assert count == 3
    by_ext = {j["external_id"]: j for j in jobs}

    # dummy-1: Accepted
    assert by_ext["dummy-1"]["prefilter_status"] == "accepted"

    # dummy-2: Filtered by title (werkstudent / assistant)
    assert by_ext["dummy-2"]["prefilter_status"] == "filtered_title"

    # dummy-3: Filtered by blocked company
    assert by_ext["dummy-3"]["prefilter_status"] == "filtered_company"


def test_external_runner_web_routes(tmp_path: Path):
    from job_search.core.database import DatabaseManager
    from job_search.web.app import init_app
    mock_db = DatabaseManager(db_path=tmp_path / "mock_app_jobs.db")
    app = init_app(mock_db)
    client = app.test_client()

    # 1. Status route
    r_status = client.get("/runner/external/status")
    assert r_status.status_code == 200
    data = r_status.get_json()
    assert "is_running" in data
    assert "status_text" in data

    # 2. Logs route
    r_logs = client.get("/runner/external/logs")
    assert r_logs.status_code == 200
    assert "logs" in r_logs.get_json()

    # 3. Stop when not running
    r_stop = client.post("/runner/external/stop")
    assert r_stop.status_code == 302
