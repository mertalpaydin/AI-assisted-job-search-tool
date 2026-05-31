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
# Schema (new format — fresh installs)
# Existing databases are restructured by _migrate_v2().
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER UNIQUE NOT NULL,
    scraped INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    search_keyword TEXT,
    search_location_id TEXT,

    title TEXT,
    company_name TEXT,
    formattedLocation TEXT,
    country TEXT,
    listedAt INTEGER,

    is_selected INTEGER,
    cv_match_score REAL,
    german_requirement_level TEXT,
    screening_reasoning TEXT,

    workRemoteAllowed INTEGER,
    workplaceTypes TEXT,

    formattedEmploymentStatus TEXT,
    formattedExperienceLevel TEXT,
    formattedIndustries TEXT,
    formattedJobFunctions TEXT,

    company_url TEXT,
    company_staff_count INTEGER,
    company_universal_name TEXT,

    jobPostingUrl TEXT,
    jobPostingId INTEGER,
    jobState TEXT,
    originalListedAt INTEGER,
    expireAt INTEGER,
    applies INTEGER,
    views INTEGER,

    applyMethod TEXT,
    applicantTrackingSystem TEXT,

    salaryInsights TEXT,
    skillsDescription TEXT,
    inferredBenefits TEXT,
    benefitsDataSource TEXT,
    companyDescription TEXT,
    description TEXT,

    application_status TEXT,
    applied_at TIMESTAMP,
    user_cl_approved INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS screening_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    screening_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cv_match_score REAL,
    german_requirement_level TEXT,
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

APPLICATION_STATUSES = ("applied", "skipped")


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
    is_selected: bool
    reasoning: str


@dataclass
class SelectedJobRow:
    job_id: int
    title: str | None
    company_name: str | None
    formattedLocation: str | None
    jobPostingUrl: str | None
    workRemoteAllowed: int | None
    description: str | None
    application_status: str | None
    applied_at: str | None
    cv_match_score: float | None
    german_requirement_level: str | None
    is_selected: int | None
    screening_reasoning: str | None
    cover_letter_text: str | None
    generation_date: str | None
    generation_status: int | None
    user_cl_approved: int | None = None
    created_at: str | None = None


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
}

# Valid column names for the jobs table (guards against injection via field names)
_JOBS_COLUMNS: frozenset[str] = frozenset({
    "scraped", "updated_at", "search_keyword", "search_location_id",
    "title", "company_name", "formattedLocation", "country", "listedAt",
    "workRemoteAllowed", "workplaceTypes",
    "formattedEmploymentStatus", "formattedExperienceLevel",
    "formattedIndustries", "formattedJobFunctions",
    "company_url", "company_staff_count", "company_universal_name",
    "jobPostingUrl", "jobPostingId", "jobState",
    "originalListedAt", "expireAt", "applies", "views",
    "applyMethod", "applicantTrackingSystem",
    "salaryInsights", "skillsDescription", "inferredBenefits", "benefitsDataSource",
    "companyDescription", "description",
    "application_status", "applied_at",
})

# Whitelisted fields for ORDER BY (prevents SQL injection via sort params)
_SORTABLE_FIELDS: frozenset[str] = frozenset({
    "title", "company_name", "formattedLocation", "cv_match_score",
    "german_requirement_level", "listedAt", "applies", "created_at",
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
        self._migrate(conn)
        conn.close()
        logger.debug("Database schema initialized: {}", self._path)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Run all pending migrations in order."""
        self._migrate_v1(conn)
        self._migrate_v2(conn)
        self._migrate_v3(conn)

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial old schema."""
        migrations = [
            "ALTER TABLE jobs ADD COLUMN application_status TEXT",
            "ALTER TABLE jobs ADD COLUMN applied_at TIMESTAMP",
        ]
        for sql in migrations:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists or table already has new schema

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """
        Full schema restructuring:
          - Merge companies table into jobs (company_name, company_url, etc.)
          - Denormalize screening results into jobs (is_selected, cv_match_score, etc.)
          - Drop location_match from screening_results
          - Purge cover letter error rows
          - Strip 'urn:li:fs_country:' prefix from country field

        Guard: checks for company_name column. Idempotent.
        """
        cur = conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('jobs') WHERE name='company_name'"
        )
        if cur.fetchone()[0] > 0:
            return  # Already on new schema

        logger.info("Running database migration v2 — restructuring schema...")

        # Foreign key enforcement must be OFF during table restructuring.
        # SQLite requires a commit before changing this pragma.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.commit()

        # Clean up any partial state from a previously failed migration attempt
        conn.execute("DROP TABLE IF EXISTS jobs_new")
        conn.commit()

        conn.execute("""
            CREATE TABLE jobs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE NOT NULL,
                scraped INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                search_keyword TEXT,
                search_location_id TEXT,
                title TEXT,
                company_name TEXT,
                formattedLocation TEXT,
                country TEXT,
                listedAt INTEGER,
                is_selected INTEGER,
                cv_match_score REAL,
                german_requirement_level TEXT,
                screening_reasoning TEXT,
                workRemoteAllowed INTEGER,
                workplaceTypes TEXT,
                formattedEmploymentStatus TEXT,
                formattedExperienceLevel TEXT,
                formattedIndustries TEXT,
                formattedJobFunctions TEXT,
                company_url TEXT,
                company_staff_count INTEGER,
                company_universal_name TEXT,
                jobPostingUrl TEXT,
                jobPostingId INTEGER,
                jobState TEXT,
                originalListedAt INTEGER,
                expireAt INTEGER,
                applies INTEGER,
                views INTEGER,
                applyMethod TEXT,
                applicantTrackingSystem TEXT,
                salaryInsights TEXT,
                skillsDescription TEXT,
                inferredBenefits TEXT,
                benefitsDataSource TEXT,
                companyDescription TEXT,
                description TEXT,
                application_status TEXT,
                applied_at TIMESTAMP
            )
        """)

        conn.execute("""
            INSERT INTO jobs_new (
                job_id, scraped, created_at, updated_at,
                search_keyword, search_location_id,
                title, company_name, formattedLocation, country, listedAt,
                is_selected, cv_match_score, german_requirement_level, screening_reasoning,
                workRemoteAllowed, workplaceTypes,
                formattedEmploymentStatus, formattedExperienceLevel,
                formattedIndustries, formattedJobFunctions,
                company_url, company_staff_count, company_universal_name,
                jobPostingUrl, jobPostingId, jobState,
                originalListedAt, expireAt, applies, views,
                applyMethod, applicantTrackingSystem,
                salaryInsights, skillsDescription, inferredBenefits, benefitsDataSource,
                companyDescription, description,
                application_status, applied_at
            )
            SELECT
                j.job_id, j.scraped, j.created_at, j.updated_at,
                j.search_keyword, j.search_location_id,
                j.title, c.name, j.formattedLocation,
                REPLACE(COALESCE(j.country, ''), 'urn:li:fs_country:', ''),
                j.listedAt,
                sr.is_selected, sr.cv_match_score, sr.german_requirement_level, sr.screening_reasoning,
                j.workRemoteAllowed, j.workplaceTypes,
                j.formattedEmploymentStatus, j.formattedExperienceLevel,
                j.formattedIndustries, j.formattedJobFunctions,
                c.url, c.staffCount, c.universalName,
                j.jobPostingUrl, j.jobPostingId, j.jobState,
                j.originalListedAt, j.expireAt, j.applies, j.views,
                j.applyMethod, j.applicantTrackingSystem,
                j.salaryInsights, j.skillsDescription, j.inferredBenefits, j.benefitsDataSource,
                j.companyDescription, j.description,
                j.application_status, j.applied_at
            FROM jobs j
            LEFT JOIN companies c ON j.company_id = c.id
            LEFT JOIN screening_results sr ON j.job_id = sr.job_id
        """)

        conn.execute("DROP TABLE IF EXISTS jobs")
        conn.execute("DROP TABLE IF EXISTS companies")
        conn.execute("ALTER TABLE jobs_new RENAME TO jobs")

        # Drop location_match (SQLite >= 3.35 only — silently skip if unsupported)
        try:
            conn.execute("ALTER TABLE screening_results DROP COLUMN location_match")
        except sqlite3.OperationalError:
            pass

        # Purge failed cover letter attempts
        conn.execute("DELETE FROM cover_letters WHERE generation_status = -1")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_scraped ON jobs(scraped)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id)")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        logger.info("Database migration v2 complete")

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Add user_cl_approved column for manual/approval-mode CL control."""
        cur = conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('jobs') WHERE name='user_cl_approved'"
        )
        if cur.fetchone()[0]:
            return  # already migrated
        conn.execute("ALTER TABLE jobs ADD COLUMN user_cl_approved INTEGER DEFAULT NULL")
        conn.commit()
        logger.info("DB migration v3: added user_cl_approved column")

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def job_exists(self, job_id: int) -> bool:
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,))
            return cur.fetchone() is not None

    def get_job_status(self, job_id: int) -> dict | None:
        """Return a minimal status dict for import UI feedback, or None if not found."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT scraped, is_selected FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        scraped, is_selected = row["scraped"], row["is_selected"]
        if scraped == -1:
            label, badge = "fetch error", "danger"
        elif scraped == 0:
            label, badge = "pending details", "secondary"
        elif is_selected is None:
            label, badge = "pending screening", "secondary"
        elif is_selected == 1:
            label, badge = "selected", "success"
        else:
            label, badge = "rejected", "warning"
        return {"job_id": job_id, "label": label, "badge": badge, "selected": is_selected == 1}

    def insert_job(self, job_id: int, keyword: str, location_id: str) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO jobs (job_id, search_keyword, search_location_id) VALUES (?, ?, ?)",
                (job_id, keyword, location_id),
            )

    def update_job_details(self, job_id: int, fields: dict[str, Any]) -> None:
        """Update job row with scraped details. Unknown field names are silently skipped."""
        sanitized = {_sanitize_field_name(k): _serialize(v) for k, v in fields.items()}
        valid = {k: v for k, v in sanitized.items() if k in _JOBS_COLUMNS}

        # Strip LinkedIn URN prefix from country field
        if "country" in valid and valid["country"]:
            valid["country"] = str(valid["country"]).replace("urn:li:fs_country:", "")

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

    def delete_job(self, job_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM screening_results WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM processing_state WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

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

    def get_jobs_pending_cover_letter(self, mode: str = "auto") -> list[int]:
        # A job needs a CL generated if:
        #   - no cover_letter row at all (cl.id IS NULL), OR
        #   - a "success" row exists but text is empty/null (stuck from a prior empty-response bug)
        # Error rows (generation_status = -1) are intentionally excluded — use
        # purge_cover_letter_errors() to reset those before re-queuing.
        stuck_or_missing = "(cl.id IS NULL OR (cl.generation_status = 1 AND (cl.cover_letter_text IS NULL OR cl.cover_letter_text = '')))"
        if mode == "user_approval":
            sql = f"""
                SELECT j.job_id FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE j.user_cl_approved = 1 AND {stuck_or_missing}
            """
        else:
            sql = f"""
                SELECT j.job_id FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE (j.is_selected = 1 OR j.user_cl_approved = 1) AND {stuck_or_missing}
            """
        with self._cursor() as cur:
            cur.execute(sql)
            return [row[0] for row in cur.fetchall()]

    def get_job_remote_info(self, job_id: int) -> tuple[str | None, int | None]:
        """Return (search_location_id, workRemoteAllowed) for a job."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT search_location_id, workRemoteAllowed FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None, None
            return row["search_location_id"], row["workRemoteAllowed"]

    def get_job_details(self, job_id: int) -> JobRow | None:
        with self._cursor() as cur:
            cur.execute("""
                SELECT job_id, title, description, formattedLocation,
                       workRemoteAllowed, formattedExperienceLevel, jobPostingUrl,
                       company_name, scraped
                FROM jobs
                WHERE job_id = ?
            """, (job_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return JobRow(**dict(row))

    # ------------------------------------------------------------------
    # Screening
    # ------------------------------------------------------------------

    def save_screening_result(self, job_id: int, result: ScreeningResult) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT INTO screening_results
                    (job_id, cv_match_score, german_requirement_level,
                     is_selected, screening_reasoning, screening_status)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(job_id) DO UPDATE SET
                    cv_match_score = excluded.cv_match_score,
                    german_requirement_level = excluded.german_requirement_level,
                    is_selected = excluded.is_selected,
                    screening_reasoning = excluded.screening_reasoning,
                    screening_status = 1,
                    screening_date = CURRENT_TIMESTAMP
            """, (
                job_id,
                result.cv_match_score,
                result.german_requirement_level,
                int(result.is_selected),
                result.reasoning,
            ))
            # Denormalize into jobs for easy single-table queries
            cur.execute("""
                UPDATE jobs SET
                    is_selected = ?,
                    cv_match_score = ?,
                    german_requirement_level = ?,
                    screening_reasoning = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
            """, (
                int(result.is_selected),
                result.cv_match_score,
                result.german_requirement_level,
                result.reasoning,
                job_id,
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
            # Remove any prior attempt rows (error or stuck null-text rows) so
            # there is always at most one cover_letter row per job.
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute("""
                INSERT INTO cover_letters
                    (job_id, cover_letter_text, gemini_model_used, api_key_index, generation_status)
                VALUES (?, ?, ?, ?, 1)
            """, (job_id, text, model, api_key_index))

    def mark_cover_letter_error(self, job_id: int, error: str, retry_count: int = 0) -> None:
        with self._cursor() as cur:
            # Replace any existing row to avoid accumulating duplicate error rows.
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute("""
                INSERT INTO cover_letters
                    (job_id, generation_status, error_message, retry_count)
                VALUES (?, -1, ?, ?)
            """, (job_id, error, retry_count))

    def purge_cover_letter_errors(self) -> list[int]:
        """Delete all failed cover letter rows. Returns the job_ids that were cleared."""
        with self._cursor() as cur:
            cur.execute("SELECT job_id FROM cover_letters WHERE generation_status = -1")
            job_ids = [row[0] for row in cur.fetchall()]
            if job_ids:
                cur.execute("DELETE FROM cover_letters WHERE generation_status = -1")
            return job_ids

    def purge_cover_letter_nulls(self) -> int:
        """Delete 'success' rows with empty text (stuck from empty-response bug)."""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM cover_letters WHERE generation_status = 1 "
                "AND (cover_letter_text IS NULL OR cover_letter_text = '')"
            )
            return cur.rowcount

    def reset_screening_errors(self) -> list[int]:
        """
        Delete failed screening rows so those jobs are re-queued.
        Returns the job_ids that were cleared.
        """
        with self._cursor() as cur:
            cur.execute("SELECT job_id FROM screening_results WHERE screening_status = -1")
            job_ids = [row[0] for row in cur.fetchall()]
            if job_ids:
                cur.execute("DELETE FROM screening_results WHERE screening_status = -1")
            return job_ids

    def reset_detail_errors(self) -> list[int]:
        """
        Reset jobs that failed detail scraping (scraped = -1) back to pending (scraped = 0)
        so they are re-queued. Returns the job_ids that were reset.
        """
        with self._cursor() as cur:
            cur.execute("SELECT job_id FROM jobs WHERE scraped = -1")
            job_ids = [row[0] for row in cur.fetchall()]
            if job_ids:
                cur.execute(
                    "UPDATE jobs SET scraped = 0, updated_at = CURRENT_TIMESTAMP WHERE scraped = -1"
                )
            return job_ids

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
            cur.execute("SELECT COUNT(*) FROM jobs WHERE is_selected = 1")
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

    def get_pipeline_stats(self) -> dict[str, int]:
        """Detailed funnel counts at every pipeline stage, including error and pending sub-states."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs")
            total_found = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 1")
            details_scraped = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
            details_pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM jobs WHERE scraped = -1")
            details_error = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM screening_results WHERE screening_status = 1")
            screened_ok = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM screening_results WHERE screening_status = -1")
            screened_error = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM jobs WHERE is_selected = 1")
            screen_pass = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE is_selected = 0 AND cv_match_score IS NOT NULL"
            )
            screen_fail = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM cover_letters WHERE generation_status = 1")
            cl_generated = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE j.is_selected = 1 AND cl.id IS NULL
            """)
            cl_pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM cover_letters WHERE generation_status = -1")
            cl_error = cur.fetchone()[0]
        return {
            "total_found": total_found,
            "details_scraped": details_scraped,
            "details_pending": details_pending,
            "details_error": details_error,
            "screened_ok": screened_ok,
            "screened_error": screened_error,
            "screen_pass": screen_pass,
            "screen_fail": screen_fail,
            "cl_generated": cl_generated,
            "cl_pending": cl_pending,
            "cl_error": cl_error,
        }

    def get_recent_stats(self, days: int = 7) -> dict[str, int]:
        """Counts for jobs found, selected, and cover letters in the last *days* days."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE created_at >= datetime('now', ?)",
                (f"-{days} days",),
            )
            found = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE is_selected = 1 AND created_at >= datetime('now', ?)",
                (f"-{days} days",),
            )
            selected = cur.fetchone()[0]
            cur.execute(
                """SELECT COUNT(*) FROM cover_letters cl
                   JOIN jobs j ON cl.job_id = j.job_id
                   WHERE cl.generation_status = 1 AND j.created_at >= datetime('now', ?)""",
                (f"-{days} days",),
            )
            cover_letters = cur.fetchone()[0]
        return {"found": found, "selected": selected, "cover_letters": cover_letters, "days": days}

    def get_search_combo_stats(self, days: int | None = None) -> list[dict]:
        """
        Per keyword+location breakdown: found, with details, screened, selected,
        selection rate %, and avg CV match score.

        If *days* is given, only jobs created within the last *days* days are included.
        """
        where = ""
        params: list = []
        if days is not None:
            where = f"WHERE created_at >= datetime('now', '-{int(days)} days')"

        with self._cursor() as cur:
            cur.execute(f"""
                SELECT
                    search_keyword,
                    search_location_id,
                    COUNT(*) AS total_found,
                    SUM(CASE WHEN scraped = 1 THEN 1 ELSE 0 END) AS with_details,
                    SUM(CASE WHEN cv_match_score IS NOT NULL THEN 1 ELSE 0 END) AS screened,
                    SUM(CASE WHEN is_selected = 1 THEN 1 ELSE 0 END) AS selected,
                    ROUND(
                        AVG(CASE WHEN cv_match_score IS NOT NULL THEN cv_match_score * 100 END)
                    ) AS avg_match_pct,
                    CASE
                        WHEN SUM(CASE WHEN cv_match_score IS NOT NULL THEN 1 ELSE 0 END) > 0
                        THEN ROUND(
                            100.0 * SUM(CASE WHEN is_selected = 1 THEN 1 ELSE 0 END) /
                            SUM(CASE WHEN cv_match_score IS NOT NULL THEN 1 ELSE 0 END)
                        )
                        ELSE NULL
                    END AS selection_rate_pct
                FROM jobs
                {where}
                GROUP BY search_keyword, search_location_id
                ORDER BY selected DESC, total_found DESC
            """, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # Application tracking
    # ------------------------------------------------------------------

    def mark_application_status(self, job_id: int, status: str | None) -> None:
        if status == "applied":
            with self._cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET application_status = ?, applied_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                    (status, job_id),
                )
        else:
            with self._cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET application_status = ? WHERE job_id = ?",
                    (status, job_id),
                )

    def set_cl_approval(self, job_id: int, approved: int | None) -> None:
        """Set user_cl_approved: 1=approved for CL, 0=rejected by user, None=clear."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET user_cl_approved = ? WHERE job_id = ?",
                (approved, job_id),
            )

    def get_jobs_pending_cl_approval(self, days: int | None = None) -> list[SelectedJobRow]:
        """Jobs screened-and-selected with no approval decision yet (for user_approval mode).

        If *days* is given, only jobs created within the last *days* days are included.
        """
        extra = ""
        if days is not None:
            extra = f"AND j.created_at >= datetime('now', '-{int(days)} days')"

        with self._cursor() as cur:
            cur.execute(f"""
                SELECT
                    j.job_id, j.title, j.company_name, j.formattedLocation,
                    j.jobPostingUrl, j.workRemoteAllowed, j.description,
                    j.application_status, j.applied_at,
                    j.cv_match_score, j.german_requirement_level, j.is_selected,
                    j.screening_reasoning,
                    cl.cover_letter_text, cl.generation_date, cl.generation_status,
                    j.user_cl_approved, j.created_at
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE j.is_selected = 1
                  AND j.user_cl_approved IS NULL
                  AND cl.id IS NULL
                  {extra}
                ORDER BY j.cv_match_score DESC NULLS LAST
            """)
            return [SelectedJobRow(**dict(row)) for row in cur.fetchall()]

    def get_application_counts(self) -> dict[str, int]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT application_status, COUNT(*) FROM jobs
                WHERE application_status IS NOT NULL
                GROUP BY application_status
            """)
            return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Selected jobs and all jobs (for export and web UI)
    # ------------------------------------------------------------------

    def get_selected_jobs(
        self,
        sort_by: str = "cv_match_score",
        sort_dir: str = "desc",
        search: str = "",
        status: str = "",
        remote_filter: str = "",   # "1" = remote only, "-1" = hide remote
        cl_ready: bool = False,
        date_from: str = "",
        date_to: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SelectedJobRow], int]:
        """Return paginated AI-selected jobs with optional filters.

        Returns (rows, total_count).  description is omitted from list rows
        (fetched only in get_selected_job) to keep the response small.
        """
        sort_col = sort_by if sort_by in _SORTABLE_FIELDS else "cv_match_score"
        sort_order = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        conditions: list[str] = ["j.is_selected = 1"]
        params: list = []

        if search:
            conditions.append(
                "(LOWER(j.title) LIKE ? OR LOWER(j.company_name) LIKE ?"
                " OR CAST(j.job_id AS TEXT) = ?)"
            )
            like = f"%{search.lower()}%"
            params.extend([like, like, search.strip()])

        if status == "pending":
            conditions.append("(j.application_status IS NULL OR j.application_status = '')")
        elif status:
            conditions.append("j.application_status = ?")
            params.append(status)

        if remote_filter == "1":
            conditions.append("j.workRemoteAllowed = 1")
        elif remote_filter == "-1":
            conditions.append("(j.workRemoteAllowed IS NULL OR j.workRemoteAllowed != 1)")

        if cl_ready:
            conditions.append("cl.cover_letter_text IS NOT NULL")

        if date_from:
            conditions.append("DATE(j.created_at) >= ?")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(j.created_at) <= ?")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self._cursor() as cur:
            # Always include the CL join so cl.* conditions in WHERE work correctly
            cur.execute(f"""
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE {where}
            """, params)
            total: int = cur.fetchone()[0]

            cur.execute(f"""
                SELECT
                    j.job_id, j.title, j.company_name, j.formattedLocation,
                    j.jobPostingUrl, j.workRemoteAllowed,
                    NULL as description,
                    j.application_status, j.applied_at,
                    j.cv_match_score, j.german_requirement_level, j.is_selected,
                    j.screening_reasoning,
                    CASE WHEN cl.cover_letter_text IS NOT NULL THEN 'yes' ELSE NULL END
                        as cover_letter_text,
                    cl.generation_date, cl.generation_status,
                    j.user_cl_approved, j.created_at
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE {where}
                ORDER BY j.{sort_col} {sort_order}
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            return [SelectedJobRow(**dict(row)) for row in cur.fetchall()], total

    def get_selected_job(self, job_id: int) -> SelectedJobRow | None:
        """Return a single job by ID (selected or not)."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT
                    j.job_id, j.title, j.company_name, j.formattedLocation,
                    j.jobPostingUrl, j.workRemoteAllowed, j.description,
                    j.application_status, j.applied_at,
                    j.cv_match_score, j.german_requirement_level, j.is_selected,
                    j.screening_reasoning,
                    cl.cover_letter_text, cl.generation_date, cl.generation_status,
                    j.user_cl_approved, j.created_at
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE j.job_id = ?
            """, (job_id,))
            row = cur.fetchone()
            return SelectedJobRow(**dict(row)) if row else None

    def get_all_jobs(
        self,
        sort_by: str = "listedAt",
        sort_dir: str = "desc",
        search: str = "",
        status: str = "",
        remote_filter: str = "",
        cl_ready: bool = False,
        date_from: str = "",
        date_to: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SelectedJobRow], int]:
        """Return paginated scraped jobs (selected or not) with optional filters.

        Returns (rows, total_count).  description is omitted from list rows.
        """
        sort_col = sort_by if sort_by in _SORTABLE_FIELDS else "listedAt"
        sort_order = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        conditions: list[str] = ["j.scraped = 1"]
        params: list = []

        if search:
            conditions.append(
                "(LOWER(j.title) LIKE ? OR LOWER(j.company_name) LIKE ?"
                " OR CAST(j.job_id AS TEXT) = ?)"
            )
            like = f"%{search.lower()}%"
            params.extend([like, like, search.strip()])

        if status == "pending":
            conditions.append("(j.application_status IS NULL OR j.application_status = '')")
        elif status:
            conditions.append("j.application_status = ?")
            params.append(status)

        if remote_filter == "1":
            conditions.append("j.workRemoteAllowed = 1")
        elif remote_filter == "-1":
            conditions.append("(j.workRemoteAllowed IS NULL OR j.workRemoteAllowed != 1)")

        if cl_ready:
            conditions.append("cl.cover_letter_text IS NOT NULL")

        if date_from:
            conditions.append("DATE(j.created_at) >= ?")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(j.created_at) <= ?")
            params.append(date_to)

        where = " AND ".join(conditions)

        with self._cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE {where}
            """, params)
            total: int = cur.fetchone()[0]

            cur.execute(f"""
                SELECT
                    j.job_id, j.title, j.company_name, j.formattedLocation,
                    j.jobPostingUrl, j.workRemoteAllowed,
                    NULL as description,
                    j.application_status, j.applied_at,
                    j.cv_match_score, j.german_requirement_level, j.is_selected,
                    j.screening_reasoning,
                    CASE WHEN cl.cover_letter_text IS NOT NULL THEN 'yes' ELSE NULL END
                        as cover_letter_text,
                    cl.generation_date, cl.generation_status,
                    j.user_cl_approved, j.created_at
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE {where}
                ORDER BY j.{sort_col} {sort_order} NULLS LAST
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            return [SelectedJobRow(**dict(row)) for row in cur.fetchall()], total

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
