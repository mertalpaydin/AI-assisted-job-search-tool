"""
One-time cleanup: delete cover letter rows that are truncated.

A complete cover letter ends with the candidate's name as the last non-empty
line (after stripping whitespace). Any row that doesn't match is deleted so
the job is re-queued on the next run.

Usage:
    python scripts/fix_incomplete_cover_letters.py          # dry-run (shows what would be deleted)
    python scripts/fix_incomplete_cover_letters.py --fix    # actually delete

After running with --fix:
    uv run job-search run -s cover-letter --resume
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

EXPECTED_ENDING = "Mert Alp AYDIN"


def last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find and optionally delete truncated cover letters."
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Delete incomplete rows. Without this flag, only prints a report (dry-run).",
    )
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    from job_search.core.config import load_config
    from job_search.core.database import DatabaseManager

    cfg = load_config(args.config)
    db = DatabaseManager(cfg.database.path)

    incomplete: list[tuple[int, int, str]] = []  # (row_id, job_id, last_line)

    try:
        with db._cursor() as cur:
            cur.execute(
                "SELECT id, job_id, cover_letter_text FROM cover_letters WHERE generation_status = 1"
            )
            rows = cur.fetchall()

        for row_id, job_id, text in rows:
            last = last_nonempty_line(text or "")
            if last != EXPECTED_ENDING:
                incomplete.append((row_id, job_id, last))

        if not incomplete:
            print("All cover letters look complete. Nothing to do.")
            return

        print(f"Found {len(incomplete)} incomplete cover letter(s):\n")
        for row_id, job_id, last in incomplete:
            print(f"  job_id={job_id:>12}  last line: {last!r}")

        if not args.fix:
            print(f"\nDry-run — no changes made. Re-run with --fix to delete these rows.")
            return

        with db._cursor() as cur:
            cur.executemany(
                "DELETE FROM cover_letters WHERE id = ?",
                [(row_id,) for row_id, _, _ in incomplete],
            )

        print(f"\nDeleted {len(incomplete)} row(s). Re-generate with:")
        print("  uv run job-search run -s cover-letter --resume")

    finally:
        db.close()


if __name__ == "__main__":
    main()