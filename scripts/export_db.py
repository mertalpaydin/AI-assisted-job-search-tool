"""
Export all database tables to timestamped CSV files.

Usage:
    uv run python scripts/export_db.py [--db data/jobs.db] [--out data/export/csv]

Output:
    data/export/csv/jobs_2026-04-19_14-30-00.csv
    data/export/csv/screening_results_2026-04-19_14-30-00.csv
    data/export/csv/cover_letters_2026-04-19_14-30-00.csv
    data/export/csv/api_usage_2026-04-19_14-30-00.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure CWD is the project root so default paths (data/jobs.db, data/export/) resolve correctly.
_PROJECT_ROOT = Path(__file__).parent.parent  # scripts/export_db.py → project root
if Path.cwd() != _PROJECT_ROOT:
    os.chdir(_PROJECT_ROOT)


TABLES = ["jobs", "screening_results", "cover_letters", "api_usage"]


def export_table(conn: sqlite3.Connection, table: str, out_dir: Path, timestamp: str) -> Path:
    cur = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 — table name is from whitelist
    rows = cur.fetchall()
    headers = [desc[0] for desc in cur.description]

    out_path = out_dir / f"{table}_{timestamp}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Export database tables to CSV files")
    parser.add_argument("--db", default="data/jobs.db", help="Path to SQLite database")
    parser.add_argument("--out", default="data/export/csv", help="Output directory")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        raise SystemExit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    conn = sqlite3.connect(str(db_path))
    try:
        for table in TABLES:
            try:
                out_path = export_table(conn, table, out_dir, timestamp)
                print(f"Exported {table} → {out_path}")
            except sqlite3.OperationalError as exc:
                print(f"Skipped {table}: {exc}")
    finally:
        conn.close()

    print(f"\nDone — files written to {out_dir}/")


if __name__ == "__main__":
    main()
