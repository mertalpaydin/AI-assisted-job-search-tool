"""
Delete pipeline error rows from the database so affected jobs are retried
on the next run with --resume.

Usage:
    python scripts/cleanup_errors.py
    python scripts/cleanup_errors.py --stage screening
    python scripts/cleanup_errors.py --stage details --stage cover-letter

Stages:
    details      Reset jobs stuck at detail-scraping errors (scraped = -1 → 0)
    screening    Delete screening_results rows with screening_status = -1
    cover-letter Delete cover_letters rows with generation_status = -1

After running, use:
    uv run job-search run --resume
to retry the cleared jobs.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_PROJECT_ROOT = Path(__file__).parent.parent
if Path.cwd() != _PROJECT_ROOT:
    os.chdir(_PROJECT_ROOT)
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

ALL_STAGES = ["details", "screening", "cover-letter"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clear pipeline error rows so jobs are retried on next --resume."
    )
    parser.add_argument(
        "--stage", dest="stages", action="append",
        choices=ALL_STAGES, metavar="STAGE",
        help=f"Stage to clear: {', '.join(ALL_STAGES)} (default: all). Repeat for multiple.",
    )
    parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config.yaml (default: config/config.yaml)",
    )
    args = parser.parse_args()

    stages = set(args.stages) if args.stages else set(ALL_STAGES)

    from job_search.core.config import load_config
    from job_search.core.database import DatabaseManager

    cfg = load_config(args.config)
    db = DatabaseManager(cfg.database.path)

    try:
        if "details" in stages:
            ids = db.reset_detail_errors()
            print(f"Details:      {len(ids)} job(s) reset to pending (scraped -1 -> 0)")

        if "screening" in stages:
            ids = db.reset_screening_errors()
            print(f"Screening:    {len(ids)} error row(s) deleted from screening_results")

        if "cover-letter" in stages:
            ids = db.purge_cover_letter_errors()
            print(f"Cover letter: {len(ids)} error row(s) deleted from cover_letters")

        print("\nDone. Run with --resume to retry the cleared jobs:")
        print("  uv run job-search run --resume")
    finally:
        db.close()


if __name__ == "__main__":
    main()
