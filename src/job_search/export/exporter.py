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
from datetime import datetime
from pathlib import Path

from loguru import logger

from job_search.core.database import DatabaseManager, SelectedJobRow

_DIVIDER = "=" * 72


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


def export_cover_letters(
    db: DatabaseManager,
    output_dir: str = "data/export",
    only_with_cover_letter: bool = False,
) -> dict[str, int]:
    """
    Export selected jobs to ``output_dir``.

    Returns a summary dict with keys ``exported``, ``skipped``, ``total``.
    """
    jobs = db.get_selected_jobs()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    exported = 0
    skipped = 0

    # --- CSV index (all selected jobs) ---
    index_path = out / "index.csv"
    with index_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "job_id", "title", "company", "location", "url",
            "cv_match_score", "german_requirement", "application_status",
            "cover_letter_ready", "export_file",
        ])
        for job in jobs:
            has_cl = bool(job.cover_letter_text)
            if only_with_cover_letter and not has_cl:
                skipped += 1
                writer.writerow([
                    job.job_id, job.title, job.company_name, job.formattedLocation,
                    job.jobPostingUrl,
                    f"{job.cv_match_score:.2f}" if job.cv_match_score else "",
                    job.german_requirement_level, job.application_status or "",
                    "no", "",
                ])
                continue

            company = _safe_name(job.company_name)
            title = _safe_name(job.title)
            filename = f"{company}_{title}_{job.job_id}.txt"
            filepath = out / filename

            content = _format_job_file(job)
            filepath.write_text(content, encoding="utf-8")

            writer.writerow([
                job.job_id, job.title, job.company_name, job.formattedLocation,
                job.jobPostingUrl,
                f"{job.cv_match_score:.2f}" if job.cv_match_score else "",
                job.german_requirement_level, job.application_status or "",
                "yes" if has_cl else "no", filename,
            ])
            exported += 1

    logger.info(
        "Export complete — {} files written, {} skipped → {}",
        exported, skipped, out,
    )
    return {"exported": exported, "skipped": skipped, "total": len(jobs)}
