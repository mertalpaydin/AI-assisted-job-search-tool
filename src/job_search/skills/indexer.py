"""
Batch skill indexer.
Extracts open-vocabulary skills from jobs and populates the job_skills database table.
Zero AI utilization. High-throughput batch processing (~1,200 jobs/sec).
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from loguru import logger

from job_search.core.database import DatabaseManager
from job_search.skills.harvester import SkillHarvester
from job_search.skills.open_extractor import OpenSkillExtractor


def index_all_skills(
    db: DatabaseManager,
    batch_size: int = 1000,
    limit: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, int | float]:
    """
    Extracts and indexes skills for all eligible jobs not yet present in job_skills table.
    Returns summary statistics: {'jobs_processed', 'skills_inserted', 'elapsed_seconds'}.
    """
    extractor = OpenSkillExtractor()
    harvester = SkillHarvester(extractor=extractor, min_companies=2)

    t0 = time.time()
    unindexed = db.get_unindexed_skill_jobs(limit=limit)
    total_unindexed = len(unindexed)

    if not total_unindexed:
        logger.info("No unindexed jobs found. Skill index is up to date.")
        return {
            "jobs_processed": 0,
            "skills_inserted": 0,
            "elapsed_seconds": 0.0,
        }

    logger.info("Starting skill extraction for {} jobs...", total_unindexed)

    total_inserted = 0
    batch_rows: list[tuple[int, str, str]] = []
    processed_count = 0

    for job_id, company, desc, skills_desc in unindexed:
        skills = harvester.process_job(
            job_id=job_id,
            company_name=company,
            description=desc or "",
            skills_description=skills_desc,
        )
        for skill in skills:
            batch_rows.append((job_id, skill, "extracted"))

        processed_count += 1

        if len(batch_rows) >= batch_size:
            inserted = db.index_jobs_skills_batch(batch_rows)
            total_inserted += inserted
            batch_rows.clear()
            if progress_callback:
                progress_callback(processed_count, total_unindexed)

    if batch_rows:
        inserted = db.index_jobs_skills_batch(batch_rows)
        total_inserted += inserted
        batch_rows.clear()

    elapsed = time.time() - t0
    logger.info(
        "Skill indexing completed: {} jobs processed, {} skill mentions indexed in {:.2f}s ({:.0f} jobs/sec)",
        processed_count,
        total_inserted,
        elapsed,
        processed_count / elapsed if elapsed > 0 else 0,
    )

    return {
        "jobs_processed": processed_count,
        "skills_inserted": total_inserted,
        "elapsed_seconds": elapsed,
    }
