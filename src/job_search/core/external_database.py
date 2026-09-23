"""
External database manager for non-LinkedIn job search results.
Maintains data/external_jobs.db completely separate from data/jobs.db.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
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

# Bumped when a one-off data migration runs, so it is not repeated on every start.
_SCHEMA_VERSION = 1

_MATCH_FIELDS = (
    "matched_linkedin_job_id",
    "matched_similarity",
    "matched_linkedin_status",
    "matched_linkedin_title",
    "matched_linkedin_company",
)

_LI_STATUS = "LOWER(matched_linkedin_status)"
_LI_SET = "matched_linkedin_status IS NOT NULL"

# LinkedIn-match filter key -> SQL condition. Several keys are aliases kept for
# old bookmarked URLs.
_MATCH_FILTERS: dict[str, str] = {}
for _keys, _cond in [
    (("net_new", "no"), "matched_linkedin_job_id IS NULL"),
    (("yes", "matched"), "matched_linkedin_job_id IS NOT NULL"),
    (("hide_applied", "exclude_applied", "not_applied"),
     f"(matched_linkedin_status IS NULL OR {_LI_STATUS} NOT LIKE '%applied%')"),
    (("applied", "li_applied"), f"({_LI_SET} AND {_LI_STATUS} LIKE '%applied%')"),
    (("selected", "li_selected"),
     f"({_LI_SET} AND {_LI_STATUS} LIKE '%selected%' AND {_LI_STATUS} NOT LIKE '%not selected%')"),
    (("not_selected", "li_not_selected"), f"({_LI_SET} AND {_LI_STATUS} LIKE '%not selected%')"),
    (("unscreened", "li_unscreened"), f"({_LI_SET} AND {_LI_STATUS} LIKE '%unscreened%')"),
    (("expired", "li_expired"), f"({_LI_SET} AND {_LI_STATUS} LIKE '%expired%')"),
    (("skipped", "li_skipped"), f"({_LI_SET} AND {_LI_STATUS} LIKE '%skipped%')"),
]:
    for _k in _keys:
        _MATCH_FILTERS[_k] = _cond


# Options shown in the LinkedIn-match dropdown, in display order. "yes" is the
# "All matched on LinkedIn" total.
_LINKEDIN_COUNT_KEYS = (
    "hide_applied", "yes", "net_new", "selected", "unscreened",
    "applied", "not_selected", "expired", "skipped",
)


def normalize_status(raw: str | None) -> str:
    """Map any stored or submitted status (incl. legacy 'dismissed'/'new') to APPLICATION_STATUSES."""
    st = (raw or "").strip().lower()
    if st in ("dismissed", "skipped"):
        return "skipped"
    if st in ("applied", "expired"):
        return st
    return "pending"


def linkedin_status_desc(app_status: str | None, is_selected: int | None, score: float | None) -> str:
    """Human-readable LinkedIn status stored on a matched external job."""
    if app_status and app_status != "pending":
        return f"LinkedIn: {app_status.capitalize()}"
    if is_selected == 1:
        score_str = f" ({score:.2f})" if score is not None else ""
        return f"LinkedIn: Selected{score_str}"
    if is_selected == 0:
        return "LinkedIn: Not selected"
    return "LinkedIn: Unscreened"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["application_status"] = normalize_status(d.get("application_status"))
    return d


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

        # One-off language backfill for rows saved before detection existed.
        # New rows get their language on insert, so this only runs once.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < _SCHEMA_VERSION:
            try:
                self._backfill_language(conn)
                conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            except Exception as e:
                logger.warning("Language backfill failed during schema init: {}", e)

        conn.commit()
        conn.close()
        logger.debug("External database schema initialized at {}", self._path)

    @staticmethod
    def _backfill_language(conn: sqlite3.Connection) -> None:
        from job_search.utils.language import detect_language

        rows = conn.execute(
            "SELECT id, description FROM external_jobs "
            "WHERE (detected_language IS NULL OR detected_language = '') "
            "AND description IS NOT NULL AND description != ''"
        ).fetchall()
        for r_id, r_desc in rows:
            lang, ratio = detect_language(r_desc)
            conn.execute(
                "UPDATE external_jobs SET detected_language = ?, german_stopword_ratio = ? WHERE id = ?",
                (lang, ratio, r_id),
            )
        if rows:
            logger.info("Backfilled language for {} external jobs", len(rows))

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def upsert_job(self, job_data: dict[str, Any], clear_match: bool = False) -> tuple[int, bool]:
        """
        Insert or update an external job.
        clear_match=True resets the LinkedIn match columns on an existing row
        (the posting no longer matches); otherwise None values are left alone.
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

        app_status = normalize_status(job_data.get("application_status"))

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

                if clear_match:
                    update_fields.extend(f"{field} = NULL" for field in _MATCH_FIELDS)

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
            return _row_to_dict(row) if row else None

    @staticmethod
    def _split(value: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
        """Normalize a comma string or list of filter values, dropping blanks and 'all'."""
        if not value:
            return []
        items = value.split(",") if isinstance(value, str) else value
        return [str(v).strip().lower() for v in items if v and str(v).strip().lower() not in ("", "all")]

    @classmethod
    def _where(
        cls,
        source: str | list[str] | None = None,
        application_status: str | list[str] | None = None,
        prefilter_status: str | None = "accepted",
        keyword: str | None = None,
        matched_only: bool | str | list[str] | None = None,
        search_query: str | None = None,
        language: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        exclude: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE clause for the job list.

        `exclude` names one filter group ("source", "status", "keyword",
        "matched", "language") to leave out, so the counts shown next to that
        group's options reflect every *other* active filter.
        """
        conditions: list[str] = []
        params: list[Any] = []

        if prefilter_status:
            conditions.append("prefilter_status = ?")
            params.append(prefilter_status)

        sources_list = cls._split(source) if exclude != "source" else []
        if sources_list:
            conditions.append(f"source IN ({', '.join('?' for _ in sources_list)})")
            params.extend(sources_list)

        statuses_list = cls._split(application_status) if exclude != "status" else []
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
            conditions.append(f"({' OR '.join(sub_conds)})")

        if keyword and keyword != "all" and exclude != "keyword":
            conditions.append("search_keyword = ?")
            params.append(keyword)

        if exclude != "matched" and matched_only is not None:
            if isinstance(matched_only, bool):
                conditions.append(
                    "matched_linkedin_job_id IS NOT NULL" if matched_only else "matched_linkedin_job_id IS NULL"
                )
            else:
                sub_conds = [_MATCH_FILTERS[st] for st in cls._split(matched_only) if st in _MATCH_FILTERS]
                if sub_conds:
                    conditions.append(f"({' OR '.join(sub_conds)})")

        if language and language != "all" and exclude != "language":
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

        return (f"WHERE {' AND '.join(conditions)}" if conditions else ""), params

    @staticmethod
    def _linkedin_counts(cur: sqlite3.Cursor, where_clause: str, params: list[Any]) -> dict[str, int]:
        """Count rows per LinkedIn-match filter option, using the same SQL as the filter itself."""
        keys = list(_LINKEDIN_COUNT_KEYS)
        sums = ", ".join(f"SUM(CASE WHEN {_MATCH_FILTERS[k]} THEN 1 ELSE 0 END)" for k in keys)
        cur.execute(f"SELECT {sums} FROM external_jobs {where_clause}", params)
        row = cur.fetchone()
        return {k: (row[i] or 0) for i, k in enumerate(keys)}

    def get_filter_counts(self, **filters: Any) -> dict[str, Any]:
        """Option counts for each filter group, given the other active filters.

        The number next to an option is exactly how many jobs the list shows
        once that option is ticked, instead of a whole-table total.
        """
        with self._cursor() as cur:
            where, params = self._where(**filters, exclude="source")
            cur.execute(f"SELECT source, COUNT(*) FROM external_jobs {where} GROUP BY source", params)
            by_source = dict(cur.fetchall())

            where, params = self._where(**filters, exclude="status")
            cur.execute(f"SELECT application_status, COUNT(*) FROM external_jobs {where} GROUP BY application_status", params)
            by_status = dict.fromkeys(APPLICATION_STATUSES, 0)
            for k, v in cur.fetchall():
                by_status[normalize_status(k)] += v
            by_status["all"] = sum(by_status.values())

            where, params = self._where(**filters, exclude="language")
            lang_where = f"{where} AND detected_language IS NOT NULL" if where else "WHERE detected_language IS NOT NULL"
            cur.execute(f"SELECT detected_language, COUNT(*) FROM external_jobs {lang_where} GROUP BY detected_language", params)
            by_language = dict(cur.fetchall())

            where, params = self._where(**filters, exclude="keyword")
            kw_where = f"{where} AND search_keyword IS NOT NULL" if where else "WHERE search_keyword IS NOT NULL"
            cur.execute(f"SELECT search_keyword, COUNT(*) FROM external_jobs {kw_where} GROUP BY search_keyword", params)
            by_keyword = dict(cur.fetchall())

            where, params = self._where(**filters, exclude="matched")
            by_linkedin_status = self._linkedin_counts(cur, where, params)

        return {
            "by_source": by_source,
            "by_status": by_status,
            "by_language": by_language,
            "by_keyword": by_keyword,
            "by_linkedin_status": by_linkedin_status,
        }

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
        where_clause, params = self._where(
            source=source,
            application_status=application_status,
            prefilter_status=prefilter_status,
            keyword=keyword,
            matched_only=matched_only,
            search_query=search_query,
            language=language,
            date_from=date_from,
            date_to=date_to,
        )

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
            rows = [_row_to_dict(r) for r in cur.fetchall()]

        return rows, total

    def update_application_status(self, job_id: int, status: str) -> None:
        """Update job application status using standard lingo."""
        norm_status = normalize_status(status)
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
                by_status[normalize_status(k)] += v

            cur.execute("SELECT detected_language, COUNT(*) FROM external_jobs WHERE prefilter_status = 'accepted' AND detected_language IS NOT NULL GROUP BY detected_language")
            by_language = dict(cur.fetchall())

            by_linkedin_status = self._linkedin_counts(cur, "WHERE prefilter_status = 'accepted'", [])

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

                with closing(sqlite3.connect(f"file:{li_path}?mode=ro", uri=True)) as li_conn:
                    li_conn.row_factory = sqlite3.Row
                    li_cur = li_conn.cursor()
                    updated_count = self._apply_linkedin_statuses(ext_cur, li_cur, matched_rows)
        except Exception as e:
            logger.warning("Failed syncing matched LinkedIn statuses: {}", e)

        return updated_count

    @staticmethod
    def _apply_linkedin_statuses(
        ext_cur: sqlite3.Cursor, li_cur: sqlite3.Cursor, matched_rows: list[sqlite3.Row]
    ) -> int:
        updated_count = 0
        for row in matched_rows:
            li_cur.execute(
                "SELECT application_status, is_selected, cv_match_score FROM jobs WHERE job_id = ?",
                (row["matched_linkedin_job_id"],),
            )
            li_row = li_cur.fetchone()
            if not li_row:
                continue

            new_status = linkedin_status_desc(
                li_row["application_status"], li_row["is_selected"], li_row["cv_match_score"]
            )
            if new_status != row["matched_linkedin_status"]:
                ext_cur.execute(
                    "UPDATE external_jobs SET matched_linkedin_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_status, row["id"]),
                )
                updated_count += 1
        return updated_count
