"""
Cover letter export — writes self-contained text files and a CSV index.

Each exported job produces one file:
    data/export/{company}_{title}_{job_id}.txt

The file contains a structured header (job details, URL, match score) followed
by the cover letter, so you can open a single file and have everything needed
to apply.

An index CSV (data/export/index.csv) lists all selected jobs regardless of
whether a cover letter has been generated yet.
"""
from __future__ import annotations

import csv
import re
import threading
from pathlib import Path

from loguru import logger

from job_search.core.database import DatabaseManager, SelectedJobRow

_DIVIDER = "=" * 72

# Guards concurrent writes to index.csv from background cover-letter threads
# and CLI bulk export running simultaneously.
_csv_lock = threading.Lock()

_CSV_HEADER = [
    "job_id", "title", "company", "location", "url",
    "cv_match_score", "german_requirement", "application_status",
    "cover_letter_ready", "export_file",
]


def _safe_name(text: str, max_len: int = 40) -> str:
    """Turn arbitrary text into a safe filesystem component."""
    cleaned = re.sub(r"[^\w\s-]", "", text or "").strip()
    cleaned = re.sub(r"[\s]+", "_", cleaned)
    return cleaned[:max_len] or "unknown"


def _format_job_file(job: SelectedJobRow) -> str:
    """Build the content of a single export text file."""
    lines: list[str] = []

    lines.append(_DIVIDER)
    lines.append("JOB APPLICATION PACKAGE")
    lines.append(_DIVIDER)
    lines.append(f"Job ID:       {job.job_id}")
    lines.append(f"Title:        {job.title or 'N/A'}")
    lines.append(f"Company:      {job.company_name or 'N/A'}")
    lines.append(f"Location:     {job.formattedLocation or 'N/A'}")
    remote = "Yes" if job.workRemoteAllowed else "No"
    lines.append(f"Remote:       {remote}")
    lines.append(f"URL:          {job.jobPostingUrl or 'N/A'}")
    lines.append(f"CV Match:     {job.cv_match_score:.0%}" if job.cv_match_score else "CV Match:     N/A")
    lines.append(f"German Req:   {job.german_requirement_level or 'N/A'}")
    lines.append(f"Status:       {job.application_status or 'not applied'}")
    lines.append("")
    lines.append("SCREENING NOTES:")
    lines.append(job.screening_reasoning or "N/A")
    lines.append(_DIVIDER)

    if job.cover_letter_text:
        lines.append("")
        lines.append("COVER LETTER")
        lines.append(_DIVIDER)
        lines.append("")
        lines.append(job.cover_letter_text.strip())
        lines.append("")
    else:
        lines.append("")
        lines.append("[Cover letter not yet generated]")
        lines.append("")

    if job.description:
        lines.append(_DIVIDER)
        lines.append("JOB DESCRIPTION (first 1000 chars)")
        lines.append(_DIVIDER)
        lines.append("")
        lines.append(job.description[:1000].strip())
        if len(job.description) > 1000:
            lines.append("... [truncated]")
        lines.append("")

    return "\n".join(lines)


def _make_csv_row(job: SelectedJobRow, filename: str) -> list:
    return [
        job.job_id, job.title, job.company_name, job.formattedLocation,
        job.jobPostingUrl,
        f"{job.cv_match_score:.2f}" if job.cv_match_score else "",
        job.german_requirement_level, job.application_status or "",
        "yes" if job.cover_letter_text else "no", filename,
    ]


def export_single_job(
    db: DatabaseManager,
    job_id: int,
    output_dir: str = "data/export",
) -> str | None:
    """
    Export one job's cover letter file immediately and update index.csv.

    Called automatically by the CoverLetterWorker after each successful
    generation. Thread-safe via _csv_lock.

    Returns the filename written, or None if the job is not found.
    """
    job = db.get_selected_job(job_id)
    if job is None:
        logger.warning("export_single_job: job {} not found or not selected", job_id)
        return None

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    filename = f"{_safe_name(job.company_name)}_{_safe_name(job.title)}_{job.job_id}.txt"
    (out / filename).write_text(_format_job_file(job), encoding="utf-8")

    # --- Update index.csv under lock ---
    index_path = out / "index.csv"
    new_row = _make_csv_row(job, filename)

    with _csv_lock:
        existing: list[list] = []
        replaced = False
        if index_path.exists():
            with index_path.open(newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if row and str(row[0]) == str(job_id):
                        existing.append(new_row)
                        replaced = True
                    else:
                        existing.append(row)
        if not replaced:
            if not existing:
                existing.append(_CSV_HEADER)
            existing.append(new_row)
        with index_path.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(existing)

    logger.info("Auto-exported job {} → {}", job_id, out / filename)
    return filename


def export_cover_letters(
    db: DatabaseManager,
    output_dir: str = "data/export",
    only_with_cover_letter: bool = False,
) -> dict[str, int]:
    """
    Bulk-export all selected jobs to ``output_dir``.

    Returns a summary dict with keys ``exported``, ``skipped``, ``total``.
    """
    jobs = db.get_selected_jobs()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    exported = 0
    skipped = 0
    rows: list[list] = [_CSV_HEADER]

    for job in jobs:
        has_cl = bool(job.cover_letter_text)
        if only_with_cover_letter and not has_cl:
            skipped += 1
            rows.append(_make_csv_row(job, ""))
            continue

        filename = f"{_safe_name(job.company_name)}_{_safe_name(job.title)}_{job.job_id}.txt"
        (out / filename).write_text(_format_job_file(job), encoding="utf-8")
        rows.append(_make_csv_row(job, filename))
        exported += 1

    with _csv_lock:
        with (out / "index.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

    logger.info(
        "Export complete — {} files written, {} skipped → {}",
        exported, skipped, out,
    )
    return {"exported": exported, "skipped": skipped, "total": len(jobs)}
