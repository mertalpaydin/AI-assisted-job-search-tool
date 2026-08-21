from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

from loguru import logger

from job_search.core.backup import assert_healthy

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
    archetype TEXT,
    prefilter_reason TEXT,
    batch_job_id INTEGER,

    workRemoteAllowed INTEGER,
    workplaceTypes TEXT,

    formattedEmploymentStatus TEXT,
    formattedExperienceLevel TEXT,
    formattedIndustries TEXT,
    formattedJobFunctions TEXT,

    company_url TEXT,
    company_staff_count INTEGER,
    company_staff_range_start INTEGER,
    company_staff_range_end INTEGER,
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
    user_cl_approved INTEGER DEFAULT NULL,
    last_cleaned_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS screening_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    screening_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    cv_match_score REAL,
    german_requirement_level TEXT,
    is_selected INTEGER,
    screening_reasoning TEXT,
    archetype TEXT,
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
CREATE INDEX IF NOT EXISTS idx_jobs_selected_cv ON jobs(is_selected, cv_match_score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_selected_created ON jobs(is_selected, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_created ON jobs(scraped, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_listed ON jobs(scraped, listedAt DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company_selected ON jobs(company_name, is_selected);
CREATE INDEX IF NOT EXISTS idx_jobs_remote_selected ON jobs(workRemoteAllowed, is_selected);
CREATE INDEX IF NOT EXISTS idx_jobs_german_level ON jobs(german_requirement_level);
CREATE INDEX IF NOT EXISTS idx_jobs_search_kw ON jobs(search_keyword);
CREATE INDEX IF NOT EXISTS idx_screening_status ON screening_results(screening_status);
CREATE INDEX IF NOT EXISTS idx_screening_selected ON screening_results(is_selected);
CREATE INDEX IF NOT EXISTS idx_cover_letter_status ON cover_letters(generation_status);
CREATE INDEX IF NOT EXISTS idx_processing_state_stage ON processing_state(stage, status);
CREATE TABLE IF NOT EXISTS batch_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_job_name TEXT NOT NULL UNIQUE,
    stage TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    state TEXT NOT NULL,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    collected_count INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp ON api_usage(request_timestamp);
"""

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

# Marker for "show only jobs rejected by this exact rule" in the jobs list.
PREFILTER_REASON_PREFIX = "reason:"

APPLICATION_STATUSES = ("applied", "skipped", "expired")


# Screening archetypes, mirroring the role families in config/prompts.yaml.
# "none" means the job fits no target family.
ARCHETYPES: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "none")

ARCHETYPE_LABELS: dict[str, str] = {
    "A": "Procurement x AI",
    "B": "AI Transformation",
    "C": "AI / Data Product",
    "D": "Applied Data Science",
    "E": "Generic AI / ML",
    "F": "Procurement (fallback)",
    "none": "No family",
}


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
    applyMethod: str | None = None
    archetype: str | None = None

    @property
    def is_easy_apply(self) -> bool:
        """Return True if job is an Easy Apply job on LinkedIn."""
        if not self.applyMethod:
            return False
        val = str(self.applyMethod)
        return "easyApplyUrl" in val or "OnsiteApply" in val


@dataclass
class ScreeningResult:
    cv_match_score: float
    german_requirement_level: str
    is_selected: bool
    reasoning: str
    archetype: str | None = None


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
    search_keyword: str | None = None
    user_notes: str | None = None
    applyMethod: str | None = None
    archetype: str | None = None
    prefilter_reason: str | None = None
    company_staff_count: int | None = None
    formattedIndustries: str | None = None
    company_staff_range_start: int | None = None
    company_staff_range_end: int | None = None

    @property
    def is_easy_apply(self) -> bool:
        """Return True if job is an Easy Apply job on LinkedIn."""
        if not self.applyMethod:
            return False
        val = str(self.applyMethod)
        return "easyApplyUrl" in val or "OnsiteApply" in val

    @property
    def company_size_label(self) -> str | None:
        """The size band the company declares, e.g. "10,001+" or "51–200".

        None for rows scraped before the band was captured — those have only
        the member count, which is a different measure and is labelled as such
        in the UI rather than being passed off as headcount.
        """
        start, end = self.company_staff_range_start, self.company_staff_range_end
        if start is None and end is None:
            return None
        if end is None:
            return f"{start:,}+"
        if start is None:
            return f"{end:,}"
        return f"{start:,}–{end:,}"

    @property
    def is_small_company(self) -> bool:
        """True for an employer of about 50 people or fewer.

        Reads the band first and the member count only as a fallback, matching
        how the size filter buckets — otherwise the badge and the filter would
        disagree on the same row.
        """
        if self.company_staff_range_end is not None:
            return self.company_staff_range_end <= 50
        if self.company_staff_range_start is not None:
            return False                      # the unbounded 10,001+ band
        return bool(self.company_staff_count and self.company_staff_count <= 50)


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
    "company_staff_range_start", "company_staff_range_end",
    "jobPostingUrl", "jobPostingId", "jobState",
    "originalListedAt", "expireAt", "applies", "views",
    "applyMethod", "applicantTrackingSystem",
    "salaryInsights", "skillsDescription", "inferredBenefits", "benefitsDataSource",
    "companyDescription", "description",
    "application_status", "applied_at",
})

# Company size, as a single upper bound the buckets can be cut against.
#
# The declared band wins: it is what the company says about itself and what
# its About page shows. A band with no `end` is the top one (10,001+), so it
# gets a sentinel that lands it above every threshold. Only when no band was
# ever captured — every row scraped before this was extracted — do we fall
# back to the LinkedIn member count, which measures something else entirely
# and is what put gategroup (declared 10,001+, 2,457 members) in the wrong
# bucket to begin with. NULLIF folds an undisclosed 0 into "unknown".
_SIZE_BOUND_SQL = """COALESCE(
    j.company_staff_range_end,
    CASE WHEN j.company_staff_range_start IS NOT NULL THEN 2147483647 END,
    NULLIF(j.company_staff_count, 0)
)"""

# Boundaries follow LinkedIn's own bands (1, 2-10, 11-50, 51-200, 201-500,
# 501-1000, 1001-5000, 5001-10000, 10001+) so a bucket never splits one. The
# top two are kept apart deliberately: merging them buried 264 companies of
# 5,001-10,000 inside a bucket that is 75% global corporates, and a Mittelstand
# employer of 6,000 is not the same search target as Amazon.
_SIZE_BUCKETS: dict[str, str] = {
    "micro": f"{_SIZE_BOUND_SQL} BETWEEN 1 AND 10",
    "startup": f"{_SIZE_BOUND_SQL} BETWEEN 11 AND 200",
    "mid": f"{_SIZE_BOUND_SQL} BETWEEN 201 AND 1000",
    "large": f"{_SIZE_BOUND_SQL} BETWEEN 1001 AND 5000",
    "enterprise": f"{_SIZE_BOUND_SQL} BETWEEN 5001 AND 10000",
    "global": f"{_SIZE_BOUND_SQL} > 10000",
    "unknown": f"{_SIZE_BOUND_SQL} IS NULL",
}

# Whitelisted fields for ORDER BY (prevents SQL injection via sort params)
_SORTABLE_FIELDS: frozenset[str] = frozenset({
    "title", "company_name", "formattedLocation", "cv_match_score",
    "german_requirement_level", "listedAt", "applies", "created_at",
    "archetype",
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

    def __init__(self, db_path: str, check_integrity: bool = True) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        if check_integrity:
            # Before _init_schema, which runs migrations and therefore writes.
            # Opening a damaged database and writing to it anyway is how one
            # bad file becomes several. ~0.3s on a 240MB database.
            assert_healthy(self._path)
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
        self._migrate_v4(conn)
        self._migrate_v5(conn)
        self._migrate_v6(conn)
        self._migrate_v7(conn)
        self._migrate_v8(conn)
        self._migrate_v9(conn)
        self._migrate_v10(conn)

    def _migrate_v10(self, conn: sqlite3.Connection) -> None:
        """Add the declared company size band alongside the member count.

        company_staff_count is how many LinkedIn members list the company as
        their employer, which is not headcount and can be off by orders of
        magnitude in either direction — gategroup declares 10,001+ employees
        and has 2,457 members; SAP's member count exceeds its real headcount.
        The band is what the About page shows, and what the size filter now
        buckets on.

        Existing rows keep NULL for both, and the filter falls back to the
        member count for them, so nothing disappears from the lists before a
        re-scrape fills the band in.
        """
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN company_staff_range_start INTEGER",
            "ALTER TABLE jobs ADD COLUMN company_staff_range_end INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_jobs_staff_range "
            "ON jobs(company_staff_range_end)",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def _migrate_v9(self, conn: sqlite3.Connection) -> None:
        """Add batch screening support: the batch_jobs table and the in-flight link.

        jobs.batch_job_id is the guard that stops an on-demand run re-screening
        work already sitting in a submitted batch.
        """
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN batch_job_id INTEGER",
            "CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_job_id)",
            """CREATE TABLE IF NOT EXISTS batch_jobs (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   provider_job_name TEXT NOT NULL UNIQUE,
                   stage TEXT NOT NULL,
                   request_count INTEGER NOT NULL,
                   state TEXT NOT NULL,
                   submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                   completed_at TIMESTAMP,
                   collected_count INTEGER DEFAULT 0,
                   error_message TEXT
               )""",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def _migrate_v8(self, conn: sqlite3.Connection) -> None:
        """Add prefilter_reason to jobs.

        Existing rows keep NULL, so the deterministic prefilters only ever
        apply to jobs discovered after this migration runs.
        """
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN prefilter_reason TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_prefilter ON jobs(prefilter_reason)",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def _migrate_v7(self, conn: sqlite3.Connection) -> None:
        """Add archetype column to jobs and screening_results."""
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN archetype TEXT",
            "ALTER TABLE screening_results ADD COLUMN archetype TEXT",
            "CREATE INDEX IF NOT EXISTS idx_jobs_archetype ON jobs(archetype)",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass

    def _migrate_v6(self, conn: sqlite3.Connection) -> None:
        """Add last_cleaned_at column and index to jobs table."""
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN last_cleaned_at TIMESTAMP")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_last_cleaned ON jobs(last_cleaned_at)")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v5(self, conn: sqlite3.Connection) -> None:
        """Add user_notes column to jobs table."""
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN user_notes TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Add compound performance indexes for web UI queries and pagination."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_jobs_selected_cv ON jobs(is_selected, cv_match_score DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_selected_created ON jobs(is_selected, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_scraped_created ON jobs(scraped, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_scraped_listed ON jobs(scraped, listedAt DESC)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_company_selected ON jobs(company_name, is_selected)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_remote_selected ON jobs(workRemoteAllowed, is_selected)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_german_level ON jobs(german_requirement_level)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_search_kw ON jobs(search_keyword)",
        ]
        for sql in indexes:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()

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

    def insert_job(
        self,
        job_id: int,
        keyword: str,
        location_id: str,
        prefilter_reason: str | None = None,
        title: str | None = None,
    ) -> None:
        """Insert a discovered job.

        A prefilter_reason records that the job was rejected on its title alone,
        so it is stored for auditing but never queued for details or screening.
        The title is stored with it: the rule matched on that string, and
        without it there is no way to judge afterwards whether the rule was too
        aggressive. Details scraping overwrites the title later for jobs that
        survive, so keeping it here costs nothing.
        """
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO jobs "
                "(job_id, search_keyword, search_location_id, prefilter_reason, title) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, keyword, location_id, prefilter_reason, title),
            )

    def mark_prefiltered(self, job_id: int, reason: str) -> None:
        """Flag an already-scraped job as rejected before screening."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET prefilter_reason = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE job_id = ?",
                (reason, job_id),
            )

    def get_prefilter_counts(self) -> list[dict]:
        """Return prefilter reasons with counts, most frequent first."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT prefilter_reason,
                       COUNT(*) AS n,
                       SUM(CASE WHEN scraped = 1 THEN 1 ELSE 0 END) AS scraped_n
                FROM jobs
                WHERE prefilter_reason IS NOT NULL
                GROUP BY prefilter_reason
                ORDER BY n DESC
            """)
            # stage matters for display: a title-stage rule fires on the search
            # result stub, so those rows have no description and never appear in
            # the All Jobs table, which requires scraped = 1.
            return [
                {
                    "reason": r[0],
                    "count": r[1],
                    "scraped_count": r[2],
                    "stage": "details" if r[2] else "title",
                }
                for r in cur.fetchall()
            ]

    def get_prefiltered_jobs(self, reason: str, limit: int = 500) -> list[dict]:
        """Return the jobs one prefilter rule caught, scraped or not.

        Deliberately does not join screening or cover letter tables and does not
        require scraped = 1: title-stage rejections only ever have the search
        stub, and that stub is exactly what you need to judge whether the rule
        is too aggressive.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT job_id, title, company_name, formattedLocation,
                       jobPostingUrl, created_at, scraped
                FROM jobs
                WHERE prefilter_reason = ?
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
            """, (reason, limit))
            return [dict(r) for r in cur.fetchall()]

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

    def find_jobs_by_company_names(self, names) -> list[tuple[int, str, str | None, str | None]]:
        """Return (job_id, company_name, title, application_status) for matching companies.

        Matching is case-insensitive, mirroring the DetailsWorker block-list check.
        """
        lowered = sorted({n.strip().lower() for n in names if n and n.strip()})
        if not lowered:
            return []
        out: list[tuple[int, str, str | None, str | None]] = []
        with self._cursor() as cur:
            # Chunked to stay under SQLite's bound-parameter limit.
            for i in range(0, len(lowered), 500):
                chunk = lowered[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                cur.execute(
                    f"SELECT job_id, company_name, title, application_status FROM jobs "
                    f"WHERE LOWER(company_name) IN ({placeholders}) "
                    f"ORDER BY company_name, job_id",
                    chunk,
                )
                out.extend((r[0], r[1], r[2], r[3]) for r in cur.fetchall())
        return out

    def delete_job(self, job_id: int) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM screening_results WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM processing_state WHERE job_id = ?", (job_id,))
            cur.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))

    def get_jobs_pending_details(self) -> list[int]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT job_id FROM jobs "
                "WHERE scraped = 0 AND prefilter_reason IS NULL "
                "ORDER BY created_at DESC, job_id DESC"
            )
            return [row[0] for row in cur.fetchall()]

    def get_jobs_pending_screening(self) -> list[int]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT j.job_id FROM jobs j
                LEFT JOIN screening_results sr ON j.job_id = sr.job_id
                WHERE j.scraped = 1 AND sr.id IS NULL AND j.prefilter_reason IS NULL
                  AND j.batch_job_id IS NULL
                ORDER BY COALESCE(j.workRemoteAllowed, 0) ASC, j.created_at DESC, j.job_id DESC
            """)
            return [row[0] for row in cur.fetchall()]

    def get_jobs_pending_cover_letter(self, mode: str = "auto") -> list[int]:
        # A job needs a CL generated if:
        #   - no cover_letter row at all (cl.id IS NULL), OR
        #   - a "success" row exists but text is empty/null (stuck from a prior empty-response bug)
        # Error rows (generation_status = -1) are intentionally excluded — use
        # purge_cover_letter_errors() to reset those before re-queuing.
        # In auto mode, Easy Apply jobs are skipped by default unless user explicitly approved them (user_cl_approved = 1).
        # In user_approval mode, any approved job (including Easy Apply) is queued.
        stuck_or_missing = "(cl.id IS NULL OR (cl.generation_status = 1 AND (cl.cover_letter_text IS NULL OR cl.cover_letter_text = '')))"
        not_easy_apply = "(j.applyMethod IS NULL OR NOT (j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%'))"
        if mode == "user_approval":
            sql = f"""
                SELECT j.job_id FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE j.user_cl_approved = 1 AND {stuck_or_missing}
                ORDER BY j.created_at DESC, COALESCE(j.originalListedAt, j.listedAt, j.job_id) DESC
            """
        else:
            sql = f"""
                SELECT j.job_id FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE (j.user_cl_approved = 1 OR (j.is_selected = 1 AND {not_easy_apply})) AND {stuck_or_missing}
                ORDER BY j.created_at DESC, COALESCE(j.originalListedAt, j.listedAt, j.job_id) DESC
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
                       company_name, scraped, archetype
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
                     is_selected, screening_reasoning, archetype, screening_status)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(job_id) DO UPDATE SET
                    cv_match_score = excluded.cv_match_score,
                    german_requirement_level = excluded.german_requirement_level,
                    is_selected = excluded.is_selected,
                    screening_reasoning = excluded.screening_reasoning,
                    archetype = excluded.archetype,
                    screening_status = 1,
                    screening_date = CURRENT_TIMESTAMP
            """, (
                job_id,
                result.cv_match_score,
                result.german_requirement_level,
                int(result.is_selected),
                result.reasoning,
                result.archetype,
            ))
            # Denormalize into jobs for easy single-table queries
            cur.execute("""
                UPDATE jobs SET
                    is_selected = ?,
                    cv_match_score = ?,
                    german_requirement_level = ?,
                    screening_reasoning = ?,
                    archetype = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
            """, (
                int(result.is_selected),
                result.cv_match_score,
                result.german_requirement_level,
                result.reasoning,
                result.archetype,
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

    def delete_cover_letter_record(self, job_id: int) -> None:
        """Delete cover letter row for job_id and mark user_cl_approved=0 and application_status='skipped'."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute(
                "UPDATE jobs SET user_cl_approved = 0, application_status = 'skipped', updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (job_id,),
            )

    def prepare_cover_letter_regeneration(self, job_id: int) -> None:
        """Delete cover letter row for job_id and set user_cl_approved=1 for re-queuing."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute(
                "UPDATE jobs SET user_cl_approved = 1, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (job_id,),
            )


    def mark_cover_letter_error(self, job_id: int, error: str, retry_count: int = 0) -> None:
        with self._cursor() as cur:
            # Replace any existing row to avoid accumulating duplicate error rows.
            cur.execute("DELETE FROM cover_letters WHERE job_id = ?", (job_id,))
            cur.execute("""
                INSERT INTO cover_letters
                    (job_id, generation_status, error_message, retry_count)
                VALUES (?, -1, ?, ?)
            """, (job_id, error, retry_count))

    def has_successful_cover_letter(self, job_id: int) -> bool:
        """Return True if a non-empty cover letter has already been generated for this job."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM cover_letters WHERE job_id = ? AND generation_status = 1 "
                "AND cover_letter_text IS NOT NULL AND cover_letter_text != ''",
                (job_id,),
            )
            return cur.fetchone() is not None

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
            cur.execute("""
                SELECT sr.job_id FROM screening_results sr
                LEFT JOIN jobs j ON sr.job_id = j.job_id
                WHERE sr.screening_status = -1
                ORDER BY j.created_at DESC, COALESCE(j.originalListedAt, j.listedAt, j.job_id) DESC
            """)
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
            cur.execute("SELECT job_id FROM jobs WHERE scraped = -1 ORDER BY created_at DESC, job_id DESC")
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

    def get_pipeline_stats(self, days: float | int | None = None,
                           cl_mode: str = "auto") -> dict[str, Any]:
        """Detailed funnel counts at every pipeline stage, including error and pending sub-states.

        cl_mode mirrors get_jobs_pending_cover_letter: under "user_approval" a
        cover letter is only ever generated for a job the user approved, so
        cl_pending counts those alone.
        """
        where_date = ""
        if days is not None and days > 0:
            if days == 1:
                where_date = "AND j.created_at >= datetime('now', '-1 day')"
            else:
                where_date = f"AND j.created_at >= datetime('now', '-{int(days)} days')"

        with self._cursor() as cur:
            date_cond = where_date.replace("AND j.", "WHERE ") if where_date else ""
            and_date = where_date.replace("AND j.", "AND ") if where_date else ""

            cur.execute(f"SELECT COUNT(*) FROM jobs {date_cond}")
            total_found = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM jobs WHERE scraped = 1 {and_date}")
            details_scraped = cur.fetchone()[0]

            # Prefiltered jobs are excluded from every "pending" number below.
            # They are not work waiting to happen, they are work deliberately
            # declined, and counting them made the runner overstate the backlog
            # by a factor of five.
            cur.execute(f"""
                SELECT COUNT(*) FROM jobs
                WHERE scraped = 0 AND prefilter_reason IS NULL {and_date}
            """)
            details_pending = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM jobs WHERE scraped = -1 {and_date}")
            details_error = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM screening_results sr
                JOIN jobs j ON sr.job_id = j.job_id
                WHERE sr.screening_status = 1 {where_date}
            """)
            screened_ok = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM screening_results sr
                JOIN jobs j ON sr.job_id = j.job_id
                WHERE sr.screening_status = -1 {where_date}
            """)
            screened_error = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM jobs WHERE is_selected = 1 {and_date}")
            screen_pass = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM jobs WHERE is_selected = 0 AND cv_match_score IS NOT NULL {and_date}")
            screen_fail = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM cover_letters cl
                JOIN jobs j ON cl.job_id = j.job_id
                WHERE cl.generation_status = 1 {where_date}
            """)
            cl_generated = cur.fetchone()[0]

            not_easy_apply = "(j.applyMethod IS NULL OR NOT (j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%'))"
            # Under user_approval only an approved job will ever generate, so it
            # is the only thing genuinely "pending". Under auto, selected jobs
            # (bar Easy Apply) are queued automatically, matching the old count.
            if cl_mode == "user_approval":
                cl_pending_where = "j.user_cl_approved = 1"
            else:
                cl_pending_where = f"(j.user_cl_approved = 1 OR (j.is_selected = 1 AND {not_easy_apply}))"
            cur.execute(f"""
                SELECT COUNT(*) FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE {cl_pending_where}
                  AND cl.id IS NULL AND (j.application_status IS NULL OR j.application_status = '') {where_date}
            """)
            cl_pending = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM cover_letters cl
                JOIN jobs j ON cl.job_id = j.job_id
                WHERE cl.generation_status = -1 {where_date}
            """)
            cl_error = cur.fetchone()[0]

            # Matches get_jobs_pending_screening: not prefiltered, not already
            # sitting in an open batch. Otherwise the tile and the queue the
            # coordinator actually builds disagree.
            cur.execute(f"""
                SELECT COUNT(*) FROM jobs
                WHERE scraped = 1 AND cv_match_score IS NULL
                  AND prefilter_reason IS NULL AND batch_job_id IS NULL {and_date}
            """)
            screen_pending = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM jobs
                WHERE batch_job_id IS NOT NULL AND cv_match_score IS NULL {and_date}
            """)
            screen_in_flight = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM jobs WHERE prefilter_reason IS NOT NULL {and_date}
            """)
            prefiltered_total = cur.fetchone()[0]

            cur.execute(f"SELECT COUNT(*) FROM jobs WHERE application_status = 'expired' {and_date}")
            expired_count = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM jobs j
                JOIN cover_letters cl ON j.job_id = cl.job_id
                WHERE j.is_selected = 1 AND cl.generation_status = 1 AND (j.application_status IS NULL OR j.application_status = '') {where_date}
            """)
            ready_to_apply = cur.fetchone()[0]

            cur.execute(f"""
                SELECT COUNT(*) FROM jobs
                WHERE is_selected = 1 AND cv_match_score >= 0.80 AND (application_status IS NULL OR application_status = '') {and_date}
            """)
            top_matches_pending = cur.fetchone()[0]

        pass_rate_pct = round(100.0 * screen_pass / screened_ok, 1) if screened_ok > 0 else 0.0

        return {
            "total_found": total_found,
            "details_scraped": details_scraped,
            "details_pending": details_pending,
            "details_error": details_error,
            "screened_ok": screened_ok,
            "screened_error": screened_error,
            "screen_pending": screen_pending,
            "screen_in_flight": screen_in_flight,
            "prefiltered_total": prefiltered_total,
            "screen_pass": screen_pass,
            "screen_fail": screen_fail,
            "screen_pass_rate_pct": pass_rate_pct,
            "cl_generated": cl_generated,
            "cl_pending": cl_pending,
            "cl_error": cl_error,
            "expired_count": expired_count,
            "ready_to_apply": ready_to_apply,
            "top_matches_pending": top_matches_pending,
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

    def mark_application_status_batch(self, job_ids: list[int], status: str | None) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        if status == "applied":
            with self._cursor() as cur:
                cur.execute(
                    f"UPDATE jobs SET application_status = ?, applied_at = CURRENT_TIMESTAMP WHERE job_id IN ({placeholders})",
                    [status] + list(job_ids),
                )
        else:
            with self._cursor() as cur:
                cur.execute(
                    f"UPDATE jobs SET application_status = ? WHERE job_id IN ({placeholders})",
                    [status] + list(job_ids),
                )

    def set_cl_approval_batch(self, job_ids: list[int], approved: int | None) -> None:
        if not job_ids:
            return
        placeholders = ",".join("?" for _ in job_ids)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE jobs SET user_cl_approved = ? WHERE job_id IN ({placeholders})",
                [approved] + list(job_ids),
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

    def get_application_counts(self, days: float | int | None = None) -> dict[str, int]:
        where = ""
        if days is not None and days > 0:
            if days == 1:
                where = "AND created_at >= datetime('now', '-1 day')"
            else:
                where = f"AND created_at >= datetime('now', '-{int(days)} days')"
        with self._cursor() as cur:
            cur.execute(f"""
                SELECT application_status, COUNT(*) FROM jobs
                WHERE application_status IS NOT NULL {where}
                GROUP BY application_status
            """)
            return {row[0]: row[1] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Selected jobs and all jobs (for export and web UI)
    # ------------------------------------------------------------------

    def get_company_counts(
        self,
        selected_only: bool = False,
        status: str = "",
        remote_filter: str = "",
        date_from: str = "",
        date_to: str = "",
        search: str = "",
        cl_ready: bool = False,
        exclude_companies: list[str] | None = None,
        include_companies: list[str] | None = None,
        keyword_filter: str = "",
        german_filter: str = "",
        company_search: str = "",
        limit: int | None = None,
        min_match: float | None = None,
        apply_type: str = "",
    ) -> list[tuple[str, int]]:
        """Return (company_name, job_count) sorted by count desc.

        Applies the same filters as get_selected_jobs / get_all_jobs so the
        company list always reflects what is visible in the current view.
        """
        conditions: list[str] = ["j.is_selected = 1" if selected_only else "j.scraped = 1"]
        conditions.append("j.company_name IS NOT NULL")
        conditions.append("j.company_name != ''")
        params: list = []

        if min_match is not None:
            conditions.append("j.cv_match_score >= ?")
            params.append(min_match)

        if company_search:
            conditions.append("LOWER(j.company_name) LIKE ?")
            params.append(f"%{company_search.lower()}%")

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

        if apply_type == "easy":
            conditions.append("(j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%')")
        elif apply_type == "company":
            conditions.append("(j.applyMethod IS NULL OR NOT (j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%'))")

        if date_from:
            conditions.append("DATE(j.created_at) >= ?")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(j.created_at) <= ?")
            params.append(date_to)

        if cl_ready:
            conditions.append("cl.cover_letter_text IS NOT NULL")

        if include_companies:
            placeholders = ",".join("?" * len(include_companies))
            conditions.append(f"LOWER(j.company_name) IN ({placeholders})")
            params.extend(c.lower() for c in include_companies)
        elif exclude_companies:
            placeholders = ",".join("?" * len(exclude_companies))
            conditions.append(f"(j.company_name IS NULL OR LOWER(j.company_name) NOT IN ({placeholders}))")
            params.extend(c.lower() for c in exclude_companies)

        if keyword_filter:
            conditions.append("LOWER(j.search_keyword) = LOWER(?)")
            params.append(keyword_filter)

        if german_filter == "max_low":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low')")
        elif german_filter == "max_medium":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low', 'medium')")
        elif german_filter:
            conditions.append("LOWER(j.german_requirement_level) = LOWER(?)")
            params.append(german_filter)

        where = " AND ".join(conditions)
        join = "LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1" if cl_ready else ""
        limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""

        with self._cursor() as cur:
            cur.execute(f"""
                SELECT j.company_name, COUNT(*) AS cnt
                FROM jobs j
                {join}
                WHERE {where}
                GROUP BY j.company_name
                ORDER BY cnt DESC, j.company_name ASC
                {limit_clause}
            """, params)
            return [(row[0], row[1]) for row in cur.fetchall()]

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
        exclude_companies: list[str] | None = None,
        include_companies: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        keyword_filter: str = "",
        german_filter: str = "",
        min_match: float | None = None,
        apply_type: str = "",
        archetype_filter: str = "",
        prefilter_filter: str = "",
        size_filter: str = "",
    ) -> tuple[list[SelectedJobRow], int]:
        """Return paginated AI-selected jobs with optional filters.

        Returns (rows, total_count).  description is omitted from list rows
        (fetched only in get_selected_job) to keep the response small.
        """
        sort_col = sort_by if sort_by in _SORTABLE_FIELDS else "cv_match_score"
        sort_order = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        conditions: list[str] = ["j.is_selected = 1"]
        params: list = []

        if min_match is not None:
            conditions.append("j.cv_match_score >= ?")
            params.append(min_match)

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

        if apply_type == "easy":
            conditions.append("(j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%')")
        elif apply_type == "company":
            conditions.append("(j.applyMethod IS NULL OR NOT (j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%'))")

        if cl_ready:
            conditions.append("cl.cover_letter_text IS NOT NULL")

        if date_from:
            conditions.append("DATE(j.created_at) >= ?")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(j.created_at) <= ?")
            params.append(date_to)

        if include_companies:
            placeholders = ",".join("?" * len(include_companies))
            conditions.append(f"LOWER(j.company_name) IN ({placeholders})")
            params.extend(c.lower() for c in include_companies)
        elif exclude_companies:
            placeholders = ",".join("?" * len(exclude_companies))
            conditions.append(f"(j.company_name IS NULL OR LOWER(j.company_name) NOT IN ({placeholders}))")
            params.extend(c.lower() for c in exclude_companies)

        if keyword_filter:
            conditions.append("LOWER(j.search_keyword) = LOWER(?)")
            params.append(keyword_filter)

        if german_filter == "max_low":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low')")
        elif german_filter == "max_medium":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low', 'medium')")
        elif german_filter:
            conditions.append("LOWER(j.german_requirement_level) = LOWER(?)")
            params.append(german_filter)

        if archetype_filter == "unclassified":
            conditions.append("(j.archetype IS NULL OR j.archetype = '' OR j.archetype = 'none')")
        elif archetype_filter:
            conditions.append("UPPER(j.archetype) = UPPER(?)")
            params.append(archetype_filter)

        # Company size buckets, cut against the declared band (see _SIZE_BUCKETS).
        if size_filter in _SIZE_BUCKETS:
            conditions.append(_SIZE_BUCKETS[size_filter])

        if prefilter_filter == "only":
            conditions.append("j.prefilter_reason IS NOT NULL")
        elif prefilter_filter == "hide":
            conditions.append("j.prefilter_reason IS NULL")
        elif prefilter_filter.startswith(PREFILTER_REASON_PREFIX):
            # "reason:<text>" narrows to one rule, so you can see exactly what a
            # single deny word caught before deciding to keep or drop it.
            conditions.append("j.prefilter_reason = ?")
            params.append(prefilter_filter[len(PREFILTER_REASON_PREFIX):])

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
                    j.user_cl_approved, j.created_at, j.search_keyword,
                    j.user_notes, j.applyMethod, j.archetype, j.prefilter_reason,
                    j.company_staff_count, j.formattedIndustries,
                    j.company_staff_range_start, j.company_staff_range_end
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
                    j.user_cl_approved, j.created_at, j.search_keyword,
                    j.user_notes, j.applyMethod, j.archetype, j.prefilter_reason,
                    j.company_staff_count, j.formattedIndustries,
                    j.company_staff_range_start, j.company_staff_range_end
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE j.job_id = ?
            """, (job_id,))
            row = cur.fetchone()
            return SelectedJobRow(**dict(row)) if row else None

    def update_user_notes(self, job_id: int, notes: str | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET user_notes = ? WHERE job_id = ?",
                (notes, job_id),
            )

    def get_adjacent_job_ids(self, job_id: int) -> tuple[int | None, int | None]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT job_id FROM jobs
                WHERE is_selected = 1
                ORDER BY cv_match_score DESC NULLS LAST, created_at DESC, job_id DESC
            """)
            ids = [row[0] for row in cur.fetchall()]
            if job_id not in ids:
                return None, None
            idx = ids.index(job_id)
            prev_id = ids[idx - 1] if idx > 0 else None
            next_id = ids[idx + 1] if idx < len(ids) - 1 else None
            return prev_id, next_id

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
        exclude_companies: list[str] | None = None,
        include_companies: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        keyword_filter: str = "",
        german_filter: str = "",
        min_match: float | None = None,
        apply_type: str = "",
        archetype_filter: str = "",
        prefilter_filter: str = "",
        size_filter: str = "",
    ) -> tuple[list[SelectedJobRow], int]:
        """Return paginated scraped jobs (selected or not) with optional filters.

        Returns (rows, total_count).  description is omitted from list rows.
        """
        sort_col = sort_by if sort_by in _SORTABLE_FIELDS else "listedAt"
        sort_order = "ASC" if sort_dir.upper() == "ASC" else "DESC"

        conditions: list[str] = ["j.scraped = 1"]
        params: list = []

        if min_match is not None:
            conditions.append("j.cv_match_score >= ?")
            params.append(min_match)

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

        if apply_type == "easy":
            conditions.append("(j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%')")
        elif apply_type == "company":
            conditions.append("(j.applyMethod IS NULL OR NOT (j.applyMethod LIKE '%easyApplyUrl%' OR j.applyMethod LIKE '%OnsiteApply%'))")

        if cl_ready:
            conditions.append("cl.cover_letter_text IS NOT NULL")

        if date_from:
            conditions.append("DATE(j.created_at) >= ?")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(j.created_at) <= ?")
            params.append(date_to)

        if include_companies:
            placeholders = ",".join("?" * len(include_companies))
            conditions.append(f"LOWER(j.company_name) IN ({placeholders})")
            params.extend(c.lower() for c in include_companies)
        elif exclude_companies:
            placeholders = ",".join("?" * len(exclude_companies))
            conditions.append(f"(j.company_name IS NULL OR LOWER(j.company_name) NOT IN ({placeholders}))")
            params.extend(c.lower() for c in exclude_companies)

        if keyword_filter:
            conditions.append("LOWER(j.search_keyword) = LOWER(?)")
            params.append(keyword_filter)

        if german_filter == "max_low":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low')")
        elif german_filter == "max_medium":
            conditions.append("LOWER(j.german_requirement_level) IN ('none', 'low', 'medium')")
        elif german_filter:
            conditions.append("LOWER(j.german_requirement_level) = LOWER(?)")
            params.append(german_filter)

        if archetype_filter == "unclassified":
            conditions.append("(j.archetype IS NULL OR j.archetype = '' OR j.archetype = 'none')")
        elif archetype_filter:
            conditions.append("UPPER(j.archetype) = UPPER(?)")
            params.append(archetype_filter)

        # Company size buckets, cut against the declared band (see _SIZE_BUCKETS).
        if size_filter in _SIZE_BUCKETS:
            conditions.append(_SIZE_BUCKETS[size_filter])

        if prefilter_filter == "only":
            conditions.append("j.prefilter_reason IS NOT NULL")
        elif prefilter_filter == "hide":
            conditions.append("j.prefilter_reason IS NULL")
        elif prefilter_filter.startswith(PREFILTER_REASON_PREFIX):
            # "reason:<text>" narrows to one rule, so you can see exactly what a
            # single deny word caught before deciding to keep or drop it.
            conditions.append("j.prefilter_reason = ?")
            params.append(prefilter_filter[len(PREFILTER_REASON_PREFIX):])

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
                    j.user_cl_approved, j.created_at, j.search_keyword,
                    j.user_notes, j.applyMethod, j.archetype, j.prefilter_reason,
                    j.company_staff_count, j.formattedIndustries,
                    j.company_staff_range_start, j.company_staff_range_end
                FROM jobs j
                LEFT JOIN cover_letters cl ON j.job_id = cl.job_id AND cl.generation_status = 1
                WHERE {where}
                ORDER BY j.{sort_col} {sort_order}
                LIMIT ? OFFSET ?
            """, params + [limit, offset])
            return [SelectedJobRow(**dict(row)) for row in cur.fetchall()], total

    # ------------------------------------------------------------------
    # Company size backfill
    # ------------------------------------------------------------------

    def get_companies_missing_size_band(self, limit: int | None = None) -> list[dict]:
        """Companies whose jobs predate the size band being captured.

        Grouped by universalName because that is what the company endpoint is
        keyed on: one request fills in every job row for that company. Biggest
        first, so an interrupted run has already fixed the companies that
        appear most often in the lists.
        """
        sql = """
            SELECT company_universal_name AS universal_name,
                   MIN(company_name) AS company_name,
                   COUNT(*) AS job_count
            FROM jobs
            WHERE company_universal_name IS NOT NULL
              AND company_universal_name <> ''
              AND company_staff_range_start IS NULL
              AND company_staff_range_end IS NULL
            GROUP BY company_universal_name
            ORDER BY job_count DESC, company_universal_name
        """
        params: list = []
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self._cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def save_company_size(self, universal_name: str, staff_count: int | None,
                          range_start: int | None, range_end: int | None) -> int:
        """Write one company's size onto every job row it owns.

        ``updated_at`` is deliberately left alone: the job posting was not
        re-fetched, only the company behind it, and that column is what tells
        us when a job's own details last changed.

        The member count is refreshed too, since the same response carries a
        current one — job rows scraped on different days had already drifted
        apart (gategroup held both 2,394 and 2,457). COALESCE keeps whatever
        was there if this response omits it.
        """
        with self._cursor() as cur:
            cur.execute("""
                UPDATE jobs SET
                    company_staff_range_start = ?,
                    company_staff_range_end = ?,
                    company_staff_count = COALESCE(?, company_staff_count)
                WHERE company_universal_name = ?
            """, (range_start, range_end, staff_count, universal_name))
            return cur.rowcount

    def count_jobs_without_size_band(self) -> int:
        with self._cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM jobs
                WHERE company_staff_range_start IS NULL
                  AND company_staff_range_end IS NULL
            """)
            return int(cur.fetchone()[0])

    # ------------------------------------------------------------------
    # Batch screening
    # ------------------------------------------------------------------

    def create_batch_job(self, provider_job_name: str, stage: str,
                         job_ids: list[int]) -> int:
        """Record a submitted batch and mark its jobs as in flight."""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO batch_jobs (provider_job_name, stage, request_count, state) "
                "VALUES (?, ?, ?, 'submitted')",
                (provider_job_name, stage, len(job_ids)),
            )
            batch_id = int(cur.lastrowid)
            cur.executemany(
                "UPDATE jobs SET batch_job_id = ? WHERE job_id = ?",
                [(batch_id, jid) for jid in job_ids],
            )
        return batch_id

    def get_open_batch_jobs(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT id, provider_job_name, stage, request_count, state,
                       submitted_at, collected_count,
                       CAST((julianday('now') - julianday(submitted_at)) * 24 AS REAL) AS age_hours
                FROM batch_jobs
                WHERE state = 'submitted'
                ORDER BY submitted_at
            """)
            return [dict(r) for r in cur.fetchall()]

    def get_recent_batch_jobs(self, limit: int = 20) -> list[dict]:
        with self._cursor() as cur:
            # Age freezes once the batch finishes: measure to completed_at when
            # set, else to now. Otherwise a collected batch's age keeps climbing
            # forever even though nothing more will happen to it.
            cur.execute("""
                SELECT id, provider_job_name, stage, request_count, state,
                       submitted_at, completed_at, collected_count, error_message,
                       CAST((julianday(COALESCE(completed_at, 'now')) - julianday(submitted_at)) * 24 AS REAL) AS age_hours
                FROM batch_jobs
                ORDER BY submitted_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def get_batch_job_ids(self, batch_id: int) -> list[int]:
        with self._cursor() as cur:
            cur.execute("SELECT job_id FROM jobs WHERE batch_job_id = ?", (batch_id,))
            return [r[0] for r in cur.fetchall()]

    def job_belongs_to_batch(self, job_id: int, batch_id: int) -> bool:
        """True when the job still points at this batch.

        This is the ordering guarantee for the whole batch design: a result is
        only written if the job has not since been abandoned or re-submitted,
        so a late arrival can never overwrite a fresher answer.
        """
        with self._cursor() as cur:
            cur.execute("SELECT batch_job_id FROM jobs WHERE job_id = ?", (job_id,))
            row = cur.fetchone()
            return row is not None and row[0] == batch_id

    def claim_batch_result(self, job_id: int, batch_id: int) -> bool:
        """Take exclusive ownership of one batch response, atomically.

        Reading ``job_belongs_to_batch`` and then clearing the link is two
        statements, so two collectors polling the same finished batch can both
        pass the check and both write the same row. Folding the check into the
        UPDATE's WHERE clause makes the claim indivisible: exactly one caller
        sees rowcount 1, and everyone else knows to discard its copy.

        The link is dropped as part of claiming, so a caller that then fails to
        parse the response must mark the job as an error (or leave it pending)
        rather than assume it is still in flight.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE jobs SET batch_job_id = NULL "
                "WHERE job_id = ? AND batch_job_id = ?",
                (job_id, batch_id),
            )
            return cur.rowcount == 1

    def clear_batch_link(self, job_ids: list[int]) -> None:
        """Release jobs from a batch so the normal screening path can take them."""
        with self._cursor() as cur:
            cur.executemany(
                "UPDATE jobs SET batch_job_id = NULL WHERE job_id = ?",
                [(jid,) for jid in job_ids],
            )

    def finish_batch_job(self, batch_id: int, state: str, collected: int = 0,
                         error_message: str | None = None) -> None:
        """Close a batch, adding to its collected count rather than replacing it.

        Two collectors that split a batch between them each report only their
        own share. Overwriting would leave whichever finished last on record,
        which reads as "half the results vanished" when in fact both halves
        were written.
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE batch_jobs SET state = ?, "
                "collected_count = COALESCE(collected_count, 0) + ?, "
                "error_message = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (state, collected, error_message, batch_id),
            )

    def abandon_batch_job(self, batch_id: int) -> int:
        """Give up on a batch and release its jobs for immediate screening.

        Any result that arrives later is discarded, because job_belongs_to_batch
        will no longer match.
        """
        job_ids = self.get_batch_job_ids(batch_id)
        self.clear_batch_link(job_ids)
        self.finish_batch_job(batch_id, "abandoned", error_message="abandoned by user")
        return len(job_ids)

    def get_archetype_counts(self, selected_only: bool = False) -> list[dict]:
        """Return per-archetype counts for the stats page and filter dropdowns.

        Ordered A..F, then unclassified. Jobs screened before archetypes
        existed have a NULL archetype and are grouped under "none".
        """
        where = "WHERE is_selected = 1" if selected_only else ""
        with self._cursor() as cur:
            cur.execute(f"""
                SELECT
                    COALESCE(NULLIF(archetype, ''), 'none') AS archetype,
                    COUNT(*) AS total,
                    SUM(CASE WHEN is_selected = 1 THEN 1 ELSE 0 END) AS selected,
                    SUM(CASE WHEN application_status = 'applied' THEN 1 ELSE 0 END) AS applied
                FROM jobs
                {where}
                GROUP BY 1
            """)
            rows = {r[0]: {"archetype": r[0], "total": r[1],
                           "selected": r[2] or 0, "applied": r[3] or 0}
                    for r in cur.fetchall()}
        ordered = [rows[a] for a in ARCHETYPES if a in rows and a != "none"]
        ordered.extend(r for k, r in rows.items() if k not in ARCHETYPES)
        if "none" in rows:
            ordered.append(rows["none"])
        for row in ordered:
            row["label"] = ARCHETYPE_LABELS.get(row["archetype"], row["archetype"])
        return ordered

    def get_distinct_keywords(self) -> list[str]:
        """Return a sorted list of all distinct search keywords present in the database."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT DISTINCT search_keyword 
                FROM jobs 
                WHERE search_keyword IS NOT NULL AND search_keyword != '' 
                ORDER BY search_keyword ASC
            """)
            return [row[0] for row in cur.fetchall()]

    def get_pending_jobs_for_cleaner(
        self,
        limit: int = 500,
        exclude_ids: set[int] | None = None,
        min_clean_interval_days: int = 3,
    ) -> list[dict]:
        """Return pending jobs to inspect for expired status, skipping jobs checked in the last N days."""
        with self._cursor() as cur:
            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                sql = f"""
                    SELECT job_id, jobPostingUrl
                    FROM jobs
                    WHERE application_status IS NULL
                      AND (last_cleaned_at IS NULL OR last_cleaned_at < datetime('now', '-{min_clean_interval_days} days'))
                      AND job_id NOT IN ({placeholders})
                    ORDER BY CASE WHEN is_selected = 1 THEN 0 WHEN is_selected IS NULL THEN 1 ELSE 2 END, created_at DESC, job_id DESC
                    LIMIT ?
                """
                params = list(exclude_ids) + [limit]
            else:
                sql = f"""
                    SELECT job_id, jobPostingUrl
                    FROM jobs
                    WHERE application_status IS NULL
                      AND (last_cleaned_at IS NULL OR last_cleaned_at < datetime('now', '-{min_clean_interval_days} days'))
                    ORDER BY CASE WHEN is_selected = 1 THEN 0 WHEN is_selected IS NULL THEN 1 ELSE 2 END, created_at DESC, job_id DESC
                    LIMIT ?
                """
                params = [limit]
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def mark_jobs_cleaned_batch(self, job_ids: list[int]) -> int:
        """Batch update last_cleaned_at = CURRENT_TIMESTAMP for specified job_ids."""
        if not job_ids:
            return 0
        with self._cursor() as cur:
            placeholders = ",".join("?" * len(job_ids))
            cur.execute(
                f"UPDATE jobs SET last_cleaned_at = CURRENT_TIMESTAMP WHERE job_id IN ({placeholders})",
                job_ids,
            )
            return cur.rowcount

    def mark_jobs_expired_batch(self, job_ids: list[int]) -> int:
        """Batch update application_status = 'expired' and last_cleaned_at = CURRENT_TIMESTAMP for specified job_ids."""
        if not job_ids:
            return 0
        with self._cursor() as cur:
            placeholders = ",".join("?" * len(job_ids))
            cur.execute(
                f"UPDATE jobs SET application_status = 'expired', last_cleaned_at = CURRENT_TIMESTAMP WHERE job_id IN ({placeholders})",
                job_ids,
            )
            return cur.rowcount

    def reset_all_pipeline_errors(self) -> dict[str, int]:
        """Clear all detail, screening, and cover letter pipeline errors."""
        return {
            "details": self.reset_detail_errors(),
            "screening": self.reset_screening_errors(),
            "cover_letter": self.purge_cover_letter_errors(),
        }

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
