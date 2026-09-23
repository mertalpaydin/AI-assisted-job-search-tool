"""
Cross-reference external job postings against cached LinkedIn jobs in data/jobs.db.
Provides read-only matching with normalized employer names and titles.
"""
from __future__ import annotations

import difflib
import re
import sqlite3
from pathlib import Path
from typing import Any

from loguru import logger


def normalize_text(text: str | None) -> str:
    """Normalize company name or title for fuzzy and exact matching."""
    if not text:
        return ""
    t = text.lower()
    # Remove gender indicators common in German postings
    t = re.sub(
        r"\(m/[wfd]/[dmf]\)|\(all genders\)|m/w/d|w/m/d|f/m/d|\(m/w/d\)|\[m/w/d\]|\(gn\)",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Remove common corporate entity suffixes
    t = re.sub(
        r"\b(gmbh|ag|se|co\.?\s*kg|kgaa|inc\.?|corp\.?|llc|ltd\.?|holding|group)\b",
        "",
        t,
        flags=re.IGNORECASE,
    )
    # Remove punctuation
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


class LinkedInMatcher:
    """Read-only matcher against local data/jobs.db."""

    def __init__(self, db_path: str | Path = "data/jobs.db") -> None:
        self._db_path = Path(db_path)
        self._indexed = False
        self._company_index: dict[str, list[dict[str, Any]]] = {}

    def _load_index(self) -> None:
        if self._indexed:
            return
        if not self._db_path.exists():
            logger.warning("LinkedIn database not found at {}; matching disabled.", self._db_path)
            self._indexed = True
            return

        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                """
                SELECT job_id, company_name, title, application_status, is_selected, cv_match_score
                FROM jobs
                WHERE company_name IS NOT NULL AND title IS NOT NULL
                """
            )
            rows = cur.fetchall()
            for r in rows:
                norm_c = normalize_text(r["company_name"])
                if norm_c not in self._company_index:
                    self._company_index[norm_c] = []

                # Format LinkedIn status
                app_status = r["application_status"]
                is_sel = r["is_selected"]
                score = r["cv_match_score"]

                if app_status and app_status != "pending":
                    status_desc = f"LinkedIn: {app_status.capitalize()}"
                elif is_sel == 1:
                    score_str = f" ({score:.2f})" if score is not None else ""
                    status_desc = f"LinkedIn: Selected{score_str}"
                elif is_sel == 0:
                    status_desc = "LinkedIn: Not selected"
                else:
                    status_desc = "LinkedIn: Unscreened"

                self._company_index[norm_c].append({
                    "job_id": r["job_id"],
                    "company_name": r["company_name"],
                    "title": r["title"],
                    "norm_title": normalize_text(r["title"]),
                    "status_desc": status_desc,
                })
            conn.close()
            self._indexed = True
            logger.info("LinkedInMatcher indexed {} companies from {}", len(self._company_index), self._db_path)
        except Exception as e:
            logger.warning("Failed to index LinkedIn jobs from {}: {}", self._db_path, e)
            self._indexed = True

    def find_match(
        self,
        company_name: str | None,
        title: str | None,
        threshold: float = 0.65,
    ) -> dict[str, Any] | None:
        """
        Attempt to find a matching LinkedIn job in jobs.db.
        Returns match dictionary if similarity >= threshold, else None.
        """
        self._load_index()
        if not company_name or not title or not self._company_index:
            return None

        norm_comp = normalize_text(company_name)
        norm_title = normalize_text(title)

        if not norm_comp or not norm_title:
            return None

        candidates = []
        if norm_comp in self._company_index:
            candidates.extend(self._company_index[norm_comp])
        else:
            # Fuzzy company match (contains or contained-in)
            for c_key, c_jobs in self._company_index.items():
                if norm_comp in c_key or c_key in norm_comp:
                    candidates.extend(c_jobs)

        if not candidates:
            return None

        best_score = 0.0
        best_candidate = None

        for cand in candidates:
            score = difflib.SequenceMatcher(None, norm_title, cand["norm_title"]).ratio()
            if score > best_score:
                best_score = score
                best_candidate = cand

        if best_candidate and best_score >= threshold:
            return {
                "matched_linkedin_job_id": best_candidate["job_id"],
                "matched_similarity": round(best_score, 3),
                "matched_linkedin_status": best_candidate["status_desc"],
                "matched_linkedin_title": best_candidate["title"],
                "matched_linkedin_company": best_candidate["company_name"],
            }

        return None
