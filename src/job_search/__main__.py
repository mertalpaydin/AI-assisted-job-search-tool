from __future__ import annotations

import os
import sys
from pathlib import Path

# Reconfigure stdout/stderr to UTF-8 so emoji and other non-ASCII characters in
# job titles/descriptions don't cause UnicodeEncodeError on Windows (cp1252).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure CWD is the project root so relative paths (config/, data/, logs/) work
# regardless of how the script is launched (IntelliJ, terminal, uv run, etc.).
_PROJECT_ROOT = Path(__file__).parents[2]  # src/job_search/__main__.py → project root
if Path.cwd() != _PROJECT_ROOT:
    os.chdir(_PROJECT_ROOT)

import click

from job_search.core.config import load_config
from job_search.core.database import APPLICATION_STATUSES
from job_search.utils.logging import setup_logging


@click.group()
def main() -> None:
    """AI-Assisted Job Search Tool"""


# ---------------------------------------------------------------------------
# run — main pipeline
# ---------------------------------------------------------------------------

@main.command()
@click.option("--config", default="config/config.yaml", show_default=True, help="Path to config file")
@click.option("--resume/--no-resume", default=True, show_default=True, help="Resume from last checkpoint")
@click.option("--log-level", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)")
@click.option(
    "--stages", "-s", multiple=True,
    type=click.Choice(["search", "details", "screen", "cover-letter"]),
    help="Stages to run (default: all). Repeat for multiple: -s screen -s cover-letter",
)
def run(config: str, resume: bool, log_level: str | None, stages: tuple[str, ...]) -> None:
    """Run the full job search pipeline, or a subset of stages.

    \b
    Examples:
      uv run job-search run                          # all stages
      uv run job-search run -s screen                # screen pending jobs only
      uv run job-search run -s cover-letter          # generate cover letters only
      uv run job-search run -s screen -s cover-letter
      uv run job-search run -s search -s details     # scrape only (no AI)
    """
    cfg = load_config(config)
    setup_logging(level=log_level or cfg.logging.level, log_file=cfg.logging.file)

    from job_search.orchestration.coordinator import ALL_STAGES, JobSearchCoordinator

    active_stages = set(stages) if stages else set(ALL_STAGES)
    coordinator = JobSearchCoordinator(cfg, stages=active_stages)

    if cfg.web.auto_start:
        click.echo(f"Web UI: http://{cfg.web.host}:{cfg.web.port}/  (starts with pipeline)")

    import signal
    try:
        signal.signal(signal.SIGTERM, lambda *_: coordinator.cleanup())
    except (AttributeError, OSError):
        pass  # SIGTERM not available on all Windows configurations

    try:
        coordinator.start(resume=resume)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.cleanup()


# ---------------------------------------------------------------------------
# reset-errors — clear pipeline error rows so jobs are retried on next run
# ---------------------------------------------------------------------------

@main.command("reset-errors")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option(
    "--stage", "stages", multiple=True,
    type=click.Choice(["details", "screening", "cover-letter"]),
    help="Which error type to reset (default: all). Repeat for multiple.",
)
def reset_errors(config: str, stages: tuple[str, ...]) -> None:
    """Clear pipeline error rows so affected jobs are retried on the next run.

    \b
    --stage details      Reset jobs stuck with detail-scraping errors (scraped = -1)
    --stage screening    Delete screening error rows so jobs are re-screened
    --stage cover-letter Delete failed cover letter rows
    """
    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    targets = set(stages) if stages else {"details", "screening", "cover-letter"}

    try:
        if "details" in targets:
            n = db.reset_detail_errors()
            click.echo(f"Details:      {n} job(s) reset to pending")
        if "screening" in targets:
            n = db.reset_screening_errors()
            click.echo(f"Screening:    {n} error row(s) deleted")
        if "cover-letter" in targets:
            n = db.purge_cover_letter_errors()
            click.echo(f"Cover letter: {n} error row(s) deleted")
        click.echo("Done — run with --resume to retry.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# export — write cover letter files
# ---------------------------------------------------------------------------

@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--output-dir", default="data/export", show_default=True,
              help="Directory to write export files")
@click.option("--all", "include_pending", is_flag=True, default=False,
              help="Include jobs whose cover letter is not yet generated")
def export(config: str, output_dir: str, include_pending: bool) -> None:
    """Export selected jobs and cover letters to text files + CSV index."""
    from job_search.core.database import DatabaseManager
    from job_search.export.exporter import export_cover_letters

    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)
    db = DatabaseManager(cfg.database.path)
    try:
        result = export_cover_letters(
            db,
            output_dir=output_dir,
            only_with_cover_letter=not include_pending,
        )
        click.echo(
            f"Exported {result['exported']} files  |  "
            f"Skipped {result['skipped']} (no CL)  |  "
            f"Total selected: {result['total']}"
        )
        click.echo(f"Output: {output_dir}/")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# track — mark a job's application status
# ---------------------------------------------------------------------------

@main.command()
@click.argument("job_id", type=int)
@click.argument("status", type=click.Choice(list(APPLICATION_STATUSES) + ["clear"]))
@click.option("--config", default="config/config.yaml", show_default=True)
def track(job_id: int, status: str, config: str) -> None:
    """Update application status for a job.

    STATUS: applied | rejected | interviewing | offered | clear
    """
    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    try:
        if status == "clear":
            db.mark_application_status(job_id, None)
            click.echo(f"Cleared status for job {job_id}")
        else:
            db.mark_application_status(job_id, status)
            click.echo(f"Job {job_id} marked as: {status}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# list — print selected jobs to terminal
# ---------------------------------------------------------------------------

@main.command(name="list")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--status", default=None,
              type=click.Choice(list(APPLICATION_STATUSES) + ["pending"]),
              help="Filter by application status")
def list_jobs(config: str, status: str | None) -> None:
    """List AI-selected jobs in the terminal."""
    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    try:
        jobs, _ = db.get_selected_jobs(status=status or "", limit=100_000)

        if not jobs:
            click.echo("No jobs found.")
            return

        click.echo(f"\n{'ID':>10}  {'Match':>6}  {'Status':<12}  {'CL':>3}  Job\n" + "-" * 80)
        for j in jobs:
            pct = f"{j.cv_match_score:.0%}" if j.cv_match_score is not None else "  ?"
            st = j.application_status or "pending"
            cl = "Yes" if j.cover_letter_text else " No"
            title = f"{j.title or 'N/A'} @ {j.company_name or '?'}"
            click.echo(f"{j.job_id:>10}  {pct:>6}  {st:<12}  {cl:>3}  {title}")
        click.echo()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# web — start the Flask UI
# ---------------------------------------------------------------------------

@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=5000, show_default=True)
@click.option("--debug", is_flag=True, default=False)
def web(config: str, host: str, port: int, debug: bool) -> None:
    """Start the web UI to review jobs and track applications."""
    from job_search.core.database import DatabaseManager
    from job_search.web.app import init_app

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    flask_app = init_app(db, config=cfg)
    click.echo(f"Web UI running at http://{host}:{port}/")
    flask_app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
