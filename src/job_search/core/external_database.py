"""
External database manager for non-LinkedIn job search results.
Maintains data/external_jobs.db completely separate from data/jobs.db.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

from loguru import logger

EXTERNAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS external_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    location TEXT,
    url TEXT,
    description TEXT,
    salary_info TEXT,
    posted_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    search_keyword TEXT,

    prefilter_status TEXT DEFAULT 'accepted',
    prefilter_reason TEXT,

    matched_linkedin_job_id INTEGER,
    matched_similarity REAL,
    matched_linkedin_status TEXT,
    matched_linkedin_title TEXT,
    matched_linkedin_company TEXT,

    cv_match_score REAL,
    archetype TEXT,
    screening_reasoning TEXT,

    cover_letter_text TEXT,
    application_status TEXT DEFAULT 'pending',
    applied_at TIMESTAMP,
    assistant_chat_file TEXT,

    detected_language TEXT,
    german_stopword_ratio REAL,

    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_ext_jobs_source ON external_jobs(source);
CREATE INDEX IF NOT EXISTS idx_ext_jobs_app_status ON external_jobs(application_status);
CREATE INDEX IF NOT EXISTS idx_ext_jobs_prefilter ON external_jobs(prefilter_status);
CREATE INDEX IF NOT EXISTS idx_ext_jobs_matched_id ON external_jobs(matched_linkedin_job_id);
"""

APPLICATION_STATUSES = ["pending", "applied", "skipped", "expired"]


class ExternalDatabaseManager:
    """Thread-safe SQLite manager for external jobs."""

    def __init__(self, db_path: str | Path = "data/external_jobs.db") -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                str(self._path),
                timeout=30.0,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
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
        conn = sqlite3.connect(str(self._path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(EXTERNAL_SCHEMA_SQL)

        # Inspect columns and migrate if existing db lacks them
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(external_jobs)")
        existing_cols = {row[1] for row in cur.fetchall()}
        cur.close()

        for col, col_type in [("detected_language", "TEXT"), ("german_stopword_ratio", "REAL")]:
            if col not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE external_jobs ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError:
                    pass

        # Create index after column is guaranteed to exist
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ext_jobs_lang ON external_jobs(detected_language)")

        # Auto-backfill language for existing rows with missing detected_language
        try:
            from job_search.utils.language import detect_language
            cur = conn.cursor()
            cur.execute(
                "SELECT id, description FROM external_jobs "
                "WHERE (detected_language IS NULL OR detected_language = '') "
                "AND description IS NOT NULL AND description != ''"
            )
            rows = cur.fetchall()
            for r_id, r_desc in rows:
                lang, ratio = detect_language(r_desc)
                conn.execute(
                    "UPDATE external_jobs SET detected_language = ?, german_stopword_ratio = ? WHERE id = ?",
                    (lang, ratio, r_id),
                )
            cur.close()
        except Exception as e:
            logger.warning("Language backfill failed during schema init: {}", e)

        conn.commit()
        conn.close()
        logger.debug("External database schema initialized at {}", self._path)

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def upsert_job(self, job_data: dict[str, Any]) -> tuple[int, bool]:
        """
        Insert or update an external job.
        Returns (id, is_new).
        """
        from job_search.utils.language import detect_language

        source = str(job_data.get("source", "unknown")).lower()
        external_id = str(job_data.get("external_id", "")).strip()
        title = str(job_data.get("title", "")).strip()
        company = str(job_data.get("company_name", "")).strip()

        if not external_id or not title or not company:
            raise ValueError("external_id, title, and company_name are required.")

        desc = job_data.get("description")
        if desc and "detected_language" not in job_data:
            lang, ratio = detect_language(desc)
            job_data["detected_language"] = lang
            job_data["german_stopword_ratio"] = ratio

        # Normalize application status to standard lingo
        raw_status = job_data.get("application_status", "pending")
        if raw_status in ("dismissed", "skipped"):
            app_status = "skipped"
        elif raw_status == "applied":
            app_status = "applied"
        elif raw_status == "expired":
            app_status = "expired"
        else:
            app_status = "pending"

        with self._cursor() as cur:
            cur.execute(
                "SELECT id, description, cover_letter_text, application_status FROM external_jobs WHERE source = ? AND external_id = ?",
                (source, external_id),
            )
            existing = cur.fetchone()

            if existing:
                job_id = existing["id"]
                # Update existing record preserving user actions
                update_fields = []
                params = []

                # Only update fields if new values are provided
                for field in [
                    "title", "company_name", "location", "url", "salary_info",
                    "posted_at", "search_keyword", "prefilter_status", "prefilter_reason",
                    "matched_linkedin_job_id", "matched_similarity", "matched_linkedin_status",
                    "matched_linkedin_title", "matched_linkedin_company",
                    "detected_language", "german_stopword_ratio"
                ]:
                    if field in job_data and job_data[field] is not None:
                        update_fields.append(f"{field} = ?")
                        params.append(job_data[field])

                # Update description if existing was empty and new has content
                new_desc = job_data.get("description")
                if new_desc and not existing["description"]:
                    update_fields.append("description = ?")
                    params.append(new_desc)

                if update_fields:
                    update_fields.append("updated_at = CURRENT_TIMESTAMP")
                    params.append(job_id)
                    cur.execute(
                        f"UPDATE external_jobs SET {', '.join(update_fields)} WHERE id = ?",
                        params,
                    )
                return job_id, False
            else:
                cur.execute(
                    """
                    INSERT INTO external_jobs (
                        source, external_id, title, company_name, location, url,
                        description, salary_info, posted_at, search_keyword,
                        prefilter_status, prefilter_reason,
                        matched_linkedin_job_id, matched_similarity, matched_linkedin_status,
                        matched_linkedin_title, matched_linkedin_company,
                        application_status, detected_language, german_stopword_ratio
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        external_id,
                        title,
                        company,
                        job_data.get("location"),
                        job_data.get("url"),
                        job_data.get("description"),
                        job_data.get("salary_info"),
                        job_data.get("posted_at"),
                        job_data.get("search_keyword"),
                        job_data.get("prefilter_status", "accepted"),
                        job_data.get("prefilter_reason"),
                        job_data.get("matched_linkedin_job_id"),
                        job_data.get("matched_similarity"),
                        job_data.get("matched_linkedin_status"),
                        job_data.get("matched_linkedin_title"),
                        job_data.get("matched_linkedin_company"),
                        app_status,
                        job_data.get("detected_language"),
                        job_data.get("german_stopword_ratio"),
                    ),
                )
                return cur.lastrowid, True

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        """Fetch a single external job by internal ID."""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM external_jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            # Map legacy status strings to standard lingo
            if d.get("application_status") in ("dismissed", "skipped"):
                d["application_status"] = "skipped"
            elif d.get("application_status") in ("new", None, ""):
                d["application_status"] = "pending"
            return d

    def get_jobs(
        self,
        page: int = 1,
        page_size: int = 25,
        source: str | list[str] | None = None,
        application_status: str | list[str] | None = None,
        prefilter_status: str | None = "accepted",
        keyword: str | None = None,
        matched_only: bool | None = None,
        search_query: str | None = None,
        language: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Query external jobs with filtering, search, multi-selection, and pagination.
        Returns (jobs, total_count).
        """
        conditions = []
        params: list[Any] = []

        if prefilter_status:
            conditions.append("prefilter_status = ?")
            params.append(prefilter_status)

        # Multi-select sources support (e.g. ['indeed', 'arbeitsagentur'] or 'indeed,arbeitsagentur')
        if source:
            if isinstance(source, str):
                sources_list = [s.strip().lower() for s in source.split(",") if s.strip() and s.strip().lower() != "all"]
            else:
                sources_list = [s.strip().lower() for s in source if s and s.strip().lower() != "all"]

            if sources_list:
                placeholders = ", ".join("?" for _ in sources_list)
                conditions.append(f"source IN ({placeholders})")
                params.extend(sources_list)

        # Status filter support
        if application_status:
            if isinstance(application_status, str):
                statuses_list = [st.strip().lower() for st in application_status.split(",") if st.strip() and st.strip().lower() != "all"]
            else:
                statuses_list = [st.strip().lower() for st in application_status if st and st.strip().lower() != "all"]

            if statuses_list:
                sub_conds = []
                for st in statuses_list:
                    if st in ("pending", "new"):
                        sub_conds.append("(application_status = 'pending' OR application_status = 'new' OR application_status IS NULL OR application_status = '')")
                    elif st in ("skipped", "dismissed"):
                        sub_conds.append("(application_status = 'skipped' OR application_status = 'dismissed')")
                    else:
                        sub_conds.append("application_status = ?")
                        params.append(st)
                if sub_conds:
                    conditions.append(f"({' OR '.join(sub_conds)})")

        if keyword and keyword != "all":
            conditions.append("search_keyword = ?")
            params.append(keyword)

        if matched_only is not None and matched_only != "" and matched_only != "all":
            if isinstance(matched_only, bool):
                if matched_only is True:
                    conditions.append("matched_linkedin_job_id IS NOT NULL")
                else:
                    conditions.append("matched_linkedin_job_id IS NULL")
            else:
                if isinstance(matched_only, str):
                    matched_items = [m.strip().lower() for m in matched_only.split(",") if m.strip() and m.strip().lower() != "all"]
                elif isinstance(matched_only, (list, tuple, set)):
                    matched_items = [m.strip().lower() for m in matched_only if m and str(m).strip().lower() != "all"]
                else:
                    matched_items = []

                if matched_items:
                    sub_conds = []
                    for st in matched_items:
                        if st in ("net_new", "no"):
                            sub_conds.append("matched_linkedin_job_id IS NULL")
                        elif st in ("yes", "matched"):
                            sub_conds.append("matched_linkedin_job_id IS NOT NULL")
                        elif st in ("hide_applied", "exclude_applied", "not_applied"):
                            sub_conds.append("(matched_linkedin_status IS NULL OR LOWER(matched_linkedin_status) NOT LIKE '%applied%')")
                        elif st in ("applied", "li_applied"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%applied%')")
                        elif st in ("selected", "li_selected"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%selected%' AND LOWER(matched_linkedin_status) NOT LIKE '%not selected%')")
                        elif st in ("not_selected", "li_not_selected"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%not selected%')")
                        elif st in ("unscreened", "li_unscreened"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%unscreened%')")
                        elif st in ("expired", "li_expired"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%expired%')")
                        elif st in ("skipped", "li_skipped"):
                            sub_conds.append("(matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%skipped%')")
                    if sub_conds:
                        conditions.append(f"({' OR '.join(sub_conds)})")

        if language and language != "all":
            conditions.append("detected_language = ?")
            params.append(language.lower())

        if date_from:
            conditions.append("DATE(created_at) >= DATE(?)")
            params.append(date_from)

        if date_to:
            conditions.append("DATE(created_at) <= DATE(?)")
            params.append(date_to)

        if search_query and search_query.strip():
            sq = f"%{search_query.strip()}%"
            conditions.append("(title LIKE ? OR company_name LIKE ? OR location LIKE ? OR CAST(id AS TEXT) = ?)")
            params.extend([sq, sq, sq, search_query.strip()])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Validate sorting
        allowed_sorts = {
            "created_at": "created_at",
            "title": "title",
            "company_name": "company_name",
            "source": "source",
            "cv_match_score": "cv_match_score",
            "matched_similarity": "matched_similarity",
            "status": "application_status",
            "application_status": "application_status",
            "id": "id",
        }
        order_col = allowed_sorts.get(sort_by, "created_at")
        order_direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

        with self._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM external_jobs {where_clause}", params)
            total = cur.fetchone()[0]

            offset = (page - 1) * page_size
            query = f"""
                SELECT * FROM external_jobs
                {where_clause}
                ORDER BY {order_col} {order_direction}, id DESC
                LIMIT ? OFFSET ?
            """
            cur.execute(query, params + [page_size, offset])
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                if d.get("application_status") in ("dismissed", "skipped"):
                    d["application_status"] = "skipped"
                elif d.get("application_status") in ("new", None, ""):
                    d["application_status"] = "pending"
                rows.append(d)

        return rows, total

    def update_application_status(self, job_id: int, status: str) -> None:
        """Update job application status using standard lingo."""
        norm_status = status.strip().lower() if status else ""
        if norm_status in ("dismissed", "skipped"):
            norm_status = "skipped"
        elif norm_status == "applied":
            norm_status = "applied"
        elif norm_status == "expired":
            norm_status = "expired"
        elif norm_status in ("", "pending", "new", "clear"):
            norm_status = "pending"

        applied_at = datetime.now() if norm_status == "applied" else None
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE external_jobs
                SET application_status = ?, applied_at = COALESCE(?, applied_at), updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (norm_status, applied_at, job_id),
            )

    def update_screening_result(
        self,
        job_id: int,
        cv_match_score: float,
        archetype: str,
        reasoning: str,
    ) -> None:
        """Update on-demand screening result."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE external_jobs
                SET cv_match_score = ?, archetype = ?, screening_reasoning = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cv_match_score, archetype, reasoning, job_id),
            )

    def update_cover_letter(self, job_id: int, cl_text: str) -> None:
        """Save or update generated cover letter text."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE external_jobs
                SET cover_letter_text = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (cl_text, job_id),
            )

    def update_assistant_chat_file(self, job_id: int, file_path: str) -> None:
        """Store path to assistant chat file."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE external_jobs SET assistant_chat_file = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (file_path, job_id),
            )

    def get_stats(self) -> dict[str, Any]:
        """Aggregate stats for dashboard."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM external_jobs WHERE prefilter_status = 'accepted'")
            total_accepted = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM external_jobs WHERE prefilter_status != 'accepted'")
            total_prefiltered = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM external_jobs WHERE matched_linkedin_job_id IS NOT NULL AND prefilter_status = 'accepted'")
            total_matched = cur.fetchone()[0]

            cur.execute("SELECT source, COUNT(*) FROM external_jobs WHERE prefilter_status = 'accepted' GROUP BY source")
            by_source = dict(cur.fetchall())

            cur.execute("SELECT application_status, COUNT(*) FROM external_jobs WHERE prefilter_status = 'accepted' GROUP BY application_status")
            raw_by_status = dict(cur.fetchall())

            # Normalize status counts to standard lingo
            by_status: dict[str, int] = {
                "pending": 0,
                "applied": 0,
                "skipped": 0,
                "expired": 0,
            }
            for k, v in raw_by_status.items():
                if k in ("dismissed", "skipped"):
                    by_status["skipped"] += v
                elif k == "applied":
                    by_status["applied"] += v
                elif k == "expired":
                    by_status["expired"] += v
                else:
                    by_status["pending"] += v

            cur.execute("SELECT detected_language, COUNT(*) FROM external_jobs WHERE prefilter_status = 'accepted' AND detected_language IS NOT NULL GROUP BY detected_language")
            by_language = dict(cur.fetchall())

            cur.execute("""
                SELECT
                    SUM(CASE WHEN matched_linkedin_status IS NULL OR LOWER(matched_linkedin_status) NOT LIKE '%applied%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%applied%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%selected%' AND LOWER(matched_linkedin_status) NOT LIKE '%not selected%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%not selected%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%unscreened%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%expired%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_status IS NOT NULL AND LOWER(matched_linkedin_status) LIKE '%skipped%' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN matched_linkedin_job_id IS NULL THEN 1 ELSE 0 END)
                FROM external_jobs
                WHERE prefilter_status = 'accepted'
            """)
            li_row = cur.fetchone()
            by_linkedin_status = {
                "hide_applied": li_row[0] or 0,
                "applied": li_row[1] or 0,
                "selected": li_row[2] or 0,
                "not_selected": li_row[3] or 0,
                "unscreened": li_row[4] or 0,
                "expired": li_row[5] or 0,
                "skipped": li_row[6] or 0,
                "net_new": li_row[7] or 0,
            }

            cur.execute("SELECT DISTINCT search_keyword FROM external_jobs WHERE search_keyword IS NOT NULL ORDER BY search_keyword")
            keywords = [r[0] for r in cur.fetchall()]

        return {
            "total_accepted": total_accepted,
            "total_prefiltered": total_prefiltered,
            "total_matched": total_matched,
            "by_source": by_source,
            "by_status": by_status,
            "by_language": by_language,
            "by_linkedin_status": by_linkedin_status,
            "keywords": keywords,
        }

    def sync_matched_linkedin_statuses(self, linkedin_db_path: str | Path = "data/jobs.db") -> int:
        """
        Synchronize matched_linkedin_status with live records in jobs.db.
        Ensures if a job's status changed on LinkedIn (e.g. user marked Applied),
        the external match reflects that status immediately.
        """
        li_path = Path(linkedin_db_path)
        if not li_path.exists():
            return 0

        updated_count = 0
        try:
            with self._cursor() as ext_cur:
                ext_cur.execute(
                    "SELECT id, matched_linkedin_job_id, matched_linkedin_status FROM external_jobs WHERE matched_linkedin_job_id IS NOT NULL"
                )
                matched_rows = ext_cur.fetchall()
                if not matched_rows:
                    return 0

                li_conn = sqlite3.connect(f"file:{li_path}?mode=ro", uri=True)
                li_conn.row_factory = sqlite3.Row
                li_cur = li_conn.cursor()

                for row in matched_rows:
                    ext_id = row["id"]
                    li_id = row["matched_linkedin_job_id"]
                    cur_status = row["matched_linkedin_status"]

                    li_cur.execute(
                        "SELECT application_status, is_selected, cv_match_score FROM jobs WHERE job_id = ?",
                        (li_id,)
                    )
                    li_row = li_cur.fetchone()
                    if not li_row:
                        continue

                    app_status = li_row["application_status"]
                    is_sel = li_row["is_selected"]
                    score = li_row["cv_match_score"]

                    if app_status and app_status != "pending":
                        new_status = f"LinkedIn: {app_status.capitalize()}"
                    elif is_sel == 1:
                        score_str = f" ({score:.2f})" if score is not None else ""
                        new_status = f"LinkedIn: Selected{score_str}"
                    elif is_sel == 0:
                        new_status = "LinkedIn: Not selected"
                    else:
                        new_status = "LinkedIn: Unscreened"

                    if new_status != cur_status:
                        ext_cur.execute(
                            "UPDATE external_jobs SET matched_linkedin_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (new_status, ext_id),
                        )
                        updated_count += 1

                li_conn.close()
        except Exception as e:
            logger.debug("Failed syncing matched LinkedIn statuses: {}", e)

        return updated_count
