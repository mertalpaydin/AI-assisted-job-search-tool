from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from loguru import logger

# ---------------------------------------------------------------------------
# Schema
# Note: Column names from the LinkedIn discovery phase had '$' and '*'
# prefixes stripped (e.g. '$recipeTypes' → 'recipeTypes').
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_urn TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    followingInfo TEXT,
    headquarter TEXT,
    lcpTreatment INTEGER,
    name TEXT,
    specialities TEXT,
    staffCount INTEGER,
    staffCountRange TEXT,
    universalName TEXT,
    url TEXT,
    viewerFollowingJobsUpdates INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER UNIQUE NOT NULL,
    scraped INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    company_id INTEGER,

    search_keyword TEXT,
    search_location_id TEXT,

    recipeTypes TEXT,
    allJobHiringTeamMembersInjectionResult TEXT,
    applyingInfo TEXT,
    employmentStatusResolutionResult TEXT,
    savingInfo TEXT,
    standardizedTitleResolutionResult TEXT,
    allowedToEdit INTEGER,
    appeal TEXT,
    applicantTrackingSystem TEXT,
    applies INTEGER,
    applyMethod TEXT,
    benefits TEXT,
    benefitsDataSource TEXT,
    claimableByViewer INTEGER,
    closedAt TEXT,
    companyDescription TEXT,
    companyDetails TEXT,
    contentSource TEXT,
    country TEXT,
    dashEntityUrn TEXT,
    dashJobPostingCardUrn TEXT,
    degreeMatches TEXT,
    description TEXT,
    draftApplicationInfo TEXT,
    eligibleForLearningCourseRecsUpsell INTEGER,
    eligibleForReferrals INTEGER,
    eligibleForSharingProfileWithPoster INTEGER,
    employmentStatus TEXT,
    encryptedPricingParams TEXT,
    entityUrn TEXT,
    expireAt INTEGER,
    formattedEmploymentStatus TEXT,
    formattedExperienceLevel TEXT,
    formattedIndustries TEXT,
    formattedJobFunctions TEXT,
    formattedLocation TEXT,
    hiringDashboardViewEnabled INTEGER,
    hiringTeamEntitlements TEXT,
    industries TEXT,
    inferredBenefits TEXT,
    jobApplicationLimitReached INTEGER,
    jobFunctions TEXT,
    jobPosterEntitlements TEXT,
    jobPostingId INTEGER,
    jobPostingUrl TEXT,
    jobRegion TEXT,
    jobState TEXT,
    listedAt INTEGER,
    locationUrn TEXT,
    locationVisibility TEXT,
    matchType TEXT,
    messagingStatus TEXT,
    messagingToken TEXT,
    new INTEGER,
    originalListedAt INTEGER,
    ownerViewEnabled INTEGER,
    postalAddress TEXT,
    poster TEXT,
    repostedJobPosting TEXT,
    salaryInsights TEXT,
    skillMatches TEXT,
    skillsDescription TEXT,
    sourceDomain TEXT,
    standardizedAddresses TEXT,
    standardizedTitle TEXT,
    talentHubJob INTEGER,
    thirdPartySourced INTEGER,
    title TEXT,
    trackingPixelUrl TEXT,
    trackingUrn TEXT,
    trustReviewDecision TEXT,
    trustReviewSla TEXT,
    views INTEGER,
    workRemoteAllowed INTEGER,
    workplaceTypes TEXT,
    workplaceTypesResolutionResults TEXT,
    yearsOfExperienceMatch TEXT,

    FOREIGN KEY (company_id) REFERENCES companies(id)
);

CREATE TABLE IF NOT EXISTS screening_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    screening_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cv_match_score REAL,
    german_requirement_level TEXT,
    location_match INTEGER,
    is_selected INTEGER,
    screening_reasoning TEXT,
    screening_status INTEGER DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS cover_letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    generation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cover_letter_text TEXT,
    gemini_model_used TEXT,
    api_key_index INTEGER,
    generation_status INTEGER DEFAULT 0,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE TABLE IF NOT EXISTS processing_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    UNIQUE(job_id, stage)
);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_index INTEGER,
    request_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    endpoint TEXT,
    success INTEGER,
    error_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_scraped ON jobs(scraped);
CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_screening_status ON screening_results(screening_status);
CREATE INDEX IF NOT EXISTS idx_screening_selected ON screening_results(is_selected);
CREATE INDEX IF NOT EXISTS idx_cover_letter_status ON cover_letters(generation_status);
CREATE INDEX IF NOT EXISTS idx_processing_state_stage ON processing_state(stage, status);
CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(request_timestamp);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class JobRow:
    job_id: int
    title: str | None
    description: str | None
    formattedLocation: str | None
    workRemoteAllowed: int | None
    formattedExperienceLevel: str | None
    jobPostingUrl: str | None
    company_name: str | None
    scraped: int


@dataclass
class ScreeningResult:
    cv_match_score: float
    german_requirement_level: str
    location_match: bool
    is_selected: bool
    reasoning: str


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------

# Column name mapping: raw LinkedIn JSON keys → sanitized SQL column names
_FIELD_NAME_MAP: dict[str, str] = {
    "$recipeTypes": "recipeTypes",
    "*allJobHiringTeamMembersInjectionResult": "allJobHiringTeamMembersInjectionResult",
    "*applyingInfo": "applyingInfo",
    "*employmentStatusResolutionResult": "employmentStatusResolutionResult",
    "*savingInfo": "savingInfo",
    "*standardizedTitleResolutionResult": "standardizedTitleResolutionResult",
    "*followingInfo": "followingInfo",
}

# Valid column names for the jobs table (guards against injection via field names)
_JOBS_COLUMNS: frozenset[str] = frozenset({
    "scraped", "updated_at", "company_id", "search_keyword", "search_location_id",
    "recipeTypes", "allJobHiringTeamMembersInjectionResult", "applyingInfo",
    "employmentStatusResolutionResult", "savingInfo", "standardizedTitleResolutionResult",
    "allowedToEdit", "appeal", "applicantTrackingSystem", "applies", "applyMethod",
    "benefits", "benefitsDataSource", "claimableByViewer", "closedAt",
    "companyDescription", "companyDetails", "contentSource", "country",
    "dashEntityUrn", "dashJobPostingCardUrn", "degreeMatches", "description",
    "draftApplicationInfo", "eligibleForLearningCourseRecsUpsell",
    "eligibleForReferrals", "eligibleForSharingProfileWithPoster",
    "employmentStatus", "encryptedPricingParams", "entityUrn", "expireAt",
    "formattedEmploymentStatus", "formattedExperienceLevel", "formattedIndustries",
    "formattedJobFunctions", "formattedLocation", "hiringDashboardViewEnabled",
    "hiringTeamEntitlements", "industries", "inferredBenefits",
    "jobApplicationLimitReached", "jobFunctions", "jobPosterEntitlements",
    "jobPostingId", "jobPostingUrl", "jobRegion", "jobState", "listedAt",
    "locationUrn", "locationVisibility", "matchType", "messagingStatus",
    "messagingToken", "new", "originalListedAt", "ownerViewEnabled",
    "postalAddress", "poster", "repostedJobPosting", "salaryInsights",
    "skillMatches", "skillsDescription", "sourceDomain", "standardizedAddresses",
    "standardizedTitle", "talentHubJob", "thirdPartySourced", "title",
    "trackingPixelUrl", "trackingUrn", "trustReviewDecision", "trustReviewSla",
    "views", "workRemoteAllowed", "workplaceTypes", "workplaceTypesResolutionResults",
    "yearsOfExperienceMatch",
})

_COMPANIES_COLUMNS: frozenset[str] = frozenset({
    "followingInfo", "headquarter", "lcpTreatment", "name", "specialities",
    "staffCount", "staffCountRange", "universalName", "url",
    "viewerFollowingJobsUpdates", "updated_at",
})


def _sanitize_field_name(name: str) -> str:
    return _FIELD_NAME_MAP.get(name, name)


def _serialize(value: Any) -> Any:
    """Convert non-scalar values to JSON strings for SQLite storage."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


class DatabaseManager:
    """Thread-safe SQLite database manager."""

    def __init__(self, db_path: str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        conn = self._connect()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(str(self._path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()
        conn.close()
        logger.debug("Database schema initialized: {}", self._path)

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def job_exists(self, job_id: int) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
            return cur.fetchone() is not None

    def insert_job(self, job_id: int, keyword: str, location_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO jobs (job_id, search_keyword, search_location_id) VALUES (?, ?, ?)",
                (job_id, keyword, location_id),
            )

    def update_job_details(self, job_id: int, fields: dict[str, Any], company_id: int | None = None) -> None:
        """Update job row with scraped details. Unknown field names are silently skipped."""
        sanitized = {_sanitize_field_name(k): _serialize(v) for k, v in fields.items()}
        valid = {k: v for k, v in sanitized.items() if k in _JOBS_COLUMNS}
        if company_id is not None:
            valid["company_id"] = company_id
        valid["scraped"] = 1
        valid["updated_at"] = "CURRENT_TIMESTAMP"

        if not valid:
            return

        set_clause = ", ".join(f"{col} = ?" for col in valid if col != "updated_at")
        set_clause += ", updated_at = CURRENT_TIMESTAMP"
        values = [v for col, v in valid.items() if col != "updated_at"]
        values.append(job_id)

        with self._cursor() as cur:
            cur.execute(f"UPDATE jobs SET {set_clause} WHERE job_id = ?", values)

    def mark_job_error(self, job_id: int) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET scraped = -1, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (job_id,),
            )

    def get_jobs_pending_details(self) -> list[int]:
        with self._cursor() as cur:
            cur.execute("SELECT job_id FROM jobs WHERE scraped = 0")
            return [row[0] for row in cur.fetchall()]

    def get_jobs_pending_screening(self) -> list[int]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT j.job_id FROM jobs j
                LEFT JOIN screening_results sr ON j.job_id = sr.job_id
                WHERE j.scraped = 1 AND sr.id IS NULL
            """)
            return [row[0] for row in cur.fetchall()]

    def get_jobs_pending_cover_letter(self) -> list[int]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT sr.job_id FROM screening_results sr
                LEFT JOIN cover_letters cl ON sr.job_id = cl.job_id
                WHERE sr.is_selected = 1 AND cl.id IS NULL
            """)
            return [row[0] for row in cur.fetchall()]

    def get_job_details(self, job_id: int) -> JobRow | None:
        with self._cursor() as cur:
            cur.execute("""
                SELECT j.job_id, j.title, j.description, j.formattedLocation,
                       j.workRemoteAllowed, j.formattedExperienceLevel, j.jobPostingUrl,
                       c.name as company_name, j.scraped
                FROM jobs j
                LEFT JOIN companies c ON j.company_id = c.id
                WHERE j.job_id = ?
            """, (job_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return JobRow(**dict(row))

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------

    def upsert_company(self, company_urn: str, fields: dict[str, Any]) -> int:
        """Insert or update company, returns the company row id."""
        sanitized = {_sanitize_field_name(k): _serialize(v) for k, v in fields.items()}
        valid = {k: v for k, v in sanitized.items() if k in _COMPANIES_COLUMNS}

        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO companies (company_urn) VALUES (?)", (company_urn,)
            )
            if valid:
                set_clause = ", ".join(f"{col} = ?" for col in valid)
                set_clause += ", updated_at = CURRENT_TIMESTAMP"
                cur.execute(
                    f"UPDATE companies SET {set_clause} WHERE company_urn = ?",
                    [*valid.values(), company_urn],
                )
            cur.execute("SELECT id FROM companies WHERE company_urn = ?", (company_urn,))
            return cur.fetchone()[0]

    # ------------------------------------------------------------------
    # Screening
    # ------------------------------------------------------------------

    def save_screening_result(self, job_id: int, result: ScreeningResult) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO screening_results
                    (job_id, cv_match_score, german_requirement_level, location_match,
                     is_selected, screening_reasoning, screening_status)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(job_id) DO UPDATE SET
                    cv_match_score = excluded.cv_match_score,
                    german_requirement_level = excluded.german_requirement_level,
                    location_match = excluded.location_match,
                    is_selected = excluded.is_selected,
                    screening_reasoning = excluded.screening_reasoning,
                    screening_status = 1,
                    screening_date = CURRENT_TIMESTAMP
            """, (
                job_id,
                result.cv_match_score,
                result.german_requirement_level,
                int(result.location_match),
                int(result.is_selected),
                result.reasoning,
            ))

    def mark_screening_error(self, job_id: int, error: str) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO screening_results (job_id, screening_status, screening_reasoning)
                VALUES (?, -1, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    screening_status = -1,
                    screening_reasoning = excluded.screening_reasoning
            """, (job_id, error))

    # ------------------------------------------------------------------
    # Cover Letters
    # ------------------------------------------------------------------

    def save_cover_letter(
        self,
        job_id: int,
        text: str,
        model: str,
        api_key_index: int,
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO cover_letters
                    (job_id, cover_letter_text, gemini_model_used, api_key_index, generation_status)
                VALUES (?, ?, ?, ?, 1)
            """, (job_id, text, model, api_key_index))

    def mark_cover_letter_error(self, job_id: int, error: str, retry_count: int = 0) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO cover_letters
                    (job_id, generation_status, error_message, retry_count)
                VALUES (?, -1, ?, ?)
            """, (job_id, error, retry_count))

    # ------------------------------------------------------------------
    # API usage
    # ------------------------------------------------------------------

    def log_api_usage(
        self,
        api_key_index: int,
        endpoint: str,
        success: bool,
        error_type: str | None = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO api_usage (api_key_index, endpoint, success, error_type)
                VALUES (?, ?, ?, ?)
            """, (api_key_index, endpoint, int(success), error_type))

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 1")
            with_details = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM screening_results WHERE screening_status = 1")
            screened = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM screening_results WHERE is_selected = 1")
            selected = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM cover_letters WHERE generation_status = 1")
            cover_letters = cur.fetchone()[0]
        return {
            "total_jobs": total,
            "with_details": with_details,
            "screened": screened,
            "selected": selected,
            "cover_letters_generated": cover_letters,
        }

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
