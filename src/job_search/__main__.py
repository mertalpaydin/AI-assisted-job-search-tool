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
from loguru import logger

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
@click.option("--no-interactive", is_flag=True, default=False,
              help="Never open a browser. Required for scheduled runs.")
@click.option("--scheduled", is_flag=True, default=False,
              help="Mark as a scheduled run: honours the schedule pause and implies --no-interactive.")
@click.option("--max-runtime", type=float, default=None,
              help="Override execution.max_runtime_hours for this run.")
def run(config: str, resume: bool, log_level: str | None, stages: tuple[str, ...],
        no_interactive: bool, scheduled: bool, max_runtime: float | None) -> None:
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

    from job_search.core import runcontrol
    from job_search.orchestration.coordinator import ALL_STAGES, JobSearchCoordinator

    # A scheduled run checks the pause first. Auto-resume happens here: if the
    # resume moment has passed, pause_state clears the file and we continue.
    if scheduled:
        no_interactive = True
        paused = runcontrol.pause_state(cfg.schedule.pause_file)
        if paused is not None:
            remaining = runcontrol.pause_remaining(cfg.schedule.pause_file)
            click.echo(f"Schedule is paused ({remaining} remaining). Exiting.")
            return

    active_stages = set(stages) if stages else set(ALL_STAGES)
    coordinator = JobSearchCoordinator(
        cfg,
        stages=active_stages,
        interactive=not no_interactive,
        origin="scheduled" if scheduled else "manual",
        max_runtime_hours=max_runtime,
    )

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

# ---------------------------------------------------------------------------
# batch — submit and collect asynchronous screening batches
# ---------------------------------------------------------------------------

def _batch_screener(cfg):
    from job_search.ai.batch_screener import BatchScreener
    from job_search.core.config import load_secrets
    from job_search.core.database import DatabaseManager

    keys = load_secrets().gemini_api_keys
    if not keys:
        raise click.ClickException("No Gemini API keys configured in config/.env")
    db = DatabaseManager(cfg.database.path)
    return BatchScreener(cfg, db, api_key=keys[0]), db


@main.group()
def batch() -> None:
    """Asynchronous screening: half price, results within 24h."""


@batch.command("submit")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--limit", default=None, type=int, help="Cap how many pending jobs to submit")
def batch_submit(config: str, limit: int | None) -> None:
    """Submit pending screening work as a batch and exit.

    The process does not wait. Results are picked up by any later run, by the
    hourly collect task, or by 'job-search batch collect'.
    """
    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)
    screener, db = _batch_screener(cfg)
    try:
        pending = db.get_jobs_pending_screening()
        if limit:
            pending = pending[:limit]
        if not pending:
            click.echo("Nothing pending. (Jobs already out with a batch are skipped.)")
            return
        batch_id = screener.submit(pending)
        if batch_id is None:
            click.echo("Nothing eligible to submit.")
        else:
            click.echo(f"Submitted batch {batch_id} with {len(pending)} job(s).")
            click.echo("Collect later with: uv run job-search batch collect")
    finally:
        db.close()


@batch.command("collect")
@click.option("--config", default="config/config.yaml", show_default=True)
def batch_collect(config: str) -> None:
    """Poll open batches and write back any that have finished."""
    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)
    screener, db = _batch_screener(cfg)
    try:
        summary = screener.collect_all(
            stale_after_hours=cfg.screening.batch_stale_after_hours
        )
        if summary.get("skipped"):
            click.echo("Another process is already collecting batches; nothing to do.")
            return
        click.echo(
            f"Batches checked: {summary['checked']}  |  results written: {summary['collected']}"
            f"  |  still running: {summary['still_running']}  |  failed: {summary['failed']}"
        )
    finally:
        db.close()


@batch.command("list")
@click.option("--config", default="config/config.yaml", show_default=True)
def batch_list(config: str) -> None:
    """Show recent batches and their state."""
    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    try:
        rows = db.get_recent_batch_jobs()
        if not rows:
            click.echo("No batches submitted yet.")
            return
        click.echo(f"\n{'ID':>4}  {'STATE':<10}  {'JOBS':>5}  {'GOT':>5}  {'AGE':>7}  SUBMITTED")
        click.echo("-" * 70)
        for r in rows:
            click.echo(
                f"{r['id']:>4}  {r['state']:<10}  {r['request_count']:>5}  "
                f"{r['collected_count']:>5}  {r['age_hours']:>6.1f}h  {r['submitted_at']}"
            )
        click.echo()
    finally:
        db.close()


@batch.command("abandon")
@click.argument("batch_id", type=int)
@click.option("--config", default="config/config.yaml", show_default=True)
def batch_abandon(batch_id: int, config: str) -> None:
    """Give up on a batch and release its jobs for immediate screening.

    You will pay for those requests twice: the batch may still complete on the
    provider's side. Any result that arrives afterwards is discarded.
    """
    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    try:
        released = db.abandon_batch_job(batch_id)
        click.echo(f"Batch {batch_id} abandoned, {released} job(s) released for screening.")
        click.echo("Note: those requests will be billed twice if the batch completes.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# login — refresh the stored LinkedIn session (the one interactive step)
# ---------------------------------------------------------------------------

@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--force", is_flag=True, default=False,
              help="Log in again even if the stored session still works")
def login(config: str, force: bool) -> None:
    """Sign in to LinkedIn and store the session for unattended runs.

    This is the only step that needs you present, because of 2FA. Once stored,
    scheduled runs reuse the session until LinkedIn invalidates it.
    """
    from job_search.core.config import load_secrets
    from job_search.core.session_store import (
        clear_session, load_session, session_saved_at, validate_session,
    )
    from job_search.scraping.auth import get_session

    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)
    secrets = load_secrets()
    path = cfg.auth.session_file

    if force:
        clear_session(path)
    else:
        existing = load_session(path)
        if existing is not None and validate_session(existing):
            click.echo(f"Stored session is still valid (saved {session_saved_at(path)}).")
            click.echo("Use --force to sign in again anyway.")
            return

    click.echo("Opening a browser. Approve the sign-in on your phone if prompted.")
    session = get_session(
        email=secrets.linkedin_username,
        password=secrets.linkedin_password,
        session_file=path,
        interactive=True,
        interactive_timeout=cfg.auth.interactive_timeout,
    )
    if session is None:
        click.echo("Login failed. See debug/ for screenshots.")
        raise SystemExit(1)
    click.echo(f"Session stored at {path}. Scheduled runs can now go unattended.")


# ---------------------------------------------------------------------------
# session / schedule — inspect and control unattended running
# ---------------------------------------------------------------------------

@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
def status(config: str) -> None:
    """Show LinkedIn session, runner lock and schedule pause state."""
    from job_search.core import runcontrol
    from job_search.core.session_store import load_session, session_saved_at, validate_session

    cfg = load_config(config)

    session = load_session(cfg.auth.session_file)
    if session is None:
        click.echo("LinkedIn session: none stored        (run: job-search login)")
    elif validate_session(session):
        click.echo(f"LinkedIn session: valid              (saved {session_saved_at(cfg.auth.session_file)})")
    else:
        click.echo("LinkedIn session: EXPIRED            (run: job-search login)")

    holder = runcontrol.is_locked(
        cfg.execution.lock_file, cfg.execution.lock_stale_after_minutes
    )
    if holder:
        click.echo(f"Runner:           running            (pid {holder.pid}, {holder.origin}, "
                   f"{holder.age_minutes:.0f} min, stages: {holder.stages})")
    else:
        click.echo("Runner:           idle")

    remaining = runcontrol.pause_remaining(cfg.schedule.pause_file)
    click.echo(f"Schedule:         paused, {remaining} left" if remaining else "Schedule:         active")


@main.command("pause")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.argument("preset", type=click.Choice(list(("12h", "tomorrow_morning", "24h", "indefinite"))),
                required=False)
def pause_cmd(config: str, preset: str | None) -> None:
    """Suppress scheduled runs until a resume time (default: tomorrow morning)."""
    from job_search.core import runcontrol

    cfg = load_config(config)
    resume_at = runcontrol.pause_schedule(
        cfg.schedule.pause_file,
        preset=preset or cfg.schedule.default_pause,
        morning_hour=cfg.schedule.morning_resume_hour,
    )
    click.echo(f"Schedule paused until {resume_at}" if resume_at
               else "Schedule paused indefinitely. Resume with: job-search resume")


@main.command("resume")
@click.option("--config", default="config/config.yaml", show_default=True)
def resume_cmd(config: str) -> None:
    """Resume scheduled runs immediately."""
    from job_search.core import runcontrol

    cfg = load_config(config)
    runcontrol.resume_schedule(cfg.schedule.pause_file)
    click.echo("Schedule resumed.")


@main.command("stop")
@click.option("--config", default="config/config.yaml", show_default=True)
def stop_cmd(config: str) -> None:
    """Ask the running pipeline to stop gracefully, whoever started it."""
    from job_search.core import runcontrol

    cfg = load_config(config)
    holder = runcontrol.is_locked(
        cfg.execution.lock_file, cfg.execution.lock_stale_after_minutes
    )
    if holder is None:
        click.echo("Nothing is running.")
        return
    runcontrol.request_stop(cfg.execution.stop_file, reason="requested from CLI")
    click.echo(f"Stop requested for pid {holder.pid} ({holder.origin} run).")


@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--limit", default=None, type=int, help="Number of pending jobs to check for expiration (default: all pending jobs)")
@click.option("--no-interactive", is_flag=True, default=False,
              help="Never open a browser. Required for scheduled runs.")
@click.option("--scheduled", is_flag=True, default=False,
              help="Honour the schedule pause and imply --no-interactive.")
@click.option("--max-runtime", type=float, default=None,
              help="Stop the sweep gracefully after this many hours, releasing the "
                   "lock before Task Scheduler's hard limit can force-kill it.")
def clean(config: str, limit: int | None, no_interactive: bool, scheduled: bool,
          max_runtime: float | None) -> None:
    """Discover expired/closed jobs on LinkedIn and mark them as 'expired'."""
    from job_search.core import runcontrol
    from job_search.core.database import DatabaseManager
    from job_search.cleaner.cleaner import JobCleaner

    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)

    if scheduled:
        no_interactive = True
        if runcontrol.pause_state(cfg.schedule.pause_file) is not None:
            click.echo("Schedule is paused. Exiting.")
            return

    if not runcontrol.acquire_lock(
        cfg.execution.lock_file, origin="scheduled" if scheduled else "manual",
        stages="clean", stale_after_minutes=cfg.execution.lock_stale_after_minutes,
    ):
        click.echo("Another run is in progress. Exiting.")
        return

    _snapshot_before(cfg, "pre-clean")
    db = DatabaseManager(cfg.database.path)
    try:
        cleaner = JobCleaner(db)
        result = cleaner.clean_pending_jobs(limit=limit, max_runtime_hours=max_runtime)
        click.echo(f"Cleaner finished: Checked {result['checked']} jobs, marked {result['expired']} as expired.")
    finally:
        db.close()
        runcontrol.release_lock(cfg.execution.lock_file)


# ---------------------------------------------------------------------------
# backup / restore: verified snapshots
# ---------------------------------------------------------------------------

def _snapshot_manager(cfg):
    from job_search.core.backup import SnapshotManager

    b = cfg.backup
    return SnapshotManager(cfg.database.path, directory=b.dir, keep=b.keep,
                           tier_hours=tuple(b.tier_hours),
                           offsite=b.offsite_path or None)


def _snapshot_before(cfg, reason: str) -> None:
    """Snapshot ahead of a bulk or destructive command. Never fatal.

    These are the operations worth being able to undo — a purge, a mass
    expiry, a migration, a 6,500-company rewrite — and at well under a second
    there is no reason to rely on someone remembering to do it by hand.
    """
    if not getattr(cfg, "backup", None) or not cfg.backup.enabled:
        return
    try:
        _snapshot_manager(cfg).take(reason)
    except Exception as exc:
        logger.warning("Pre-operation snapshot skipped: {}", exc)


@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--list", "list_only", is_flag=True, default=False,
              help="Show existing snapshots instead of taking one.")
@click.option("--offsite", is_flag=True, default=False,
              help="Also write the compressed copy to backup.offsite_path.")
@click.option("--reason", default="manual", show_default=True,
              help="Short label recorded in the snapshot filename.")
def backup(config: str, list_only: bool, offsite: bool, reason: str) -> None:
    """Take a verified snapshot of the database, or list existing ones."""
    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)
    manager = _snapshot_manager(cfg)

    if list_only:
        snaps = manager.list()
        if not snaps:
            click.echo("No snapshots yet.")
            return
        click.echo(f"{'snapshot':<38} {'size':>8}  {'age':>10}  reason")
        for s in snaps:
            hours = s.age.total_seconds() / 3600
            age = f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"
            click.echo(f"{s.path.name:<38} {s.size_mb:>7.0f}M  {age:>10}  {s.reason}")
        return

    snap = manager.take(reason)
    if snap is None:
        raise click.ClickException(
            "No snapshot taken — the database failed its integrity check. "
            "See the log, and restore with: uv run job-search restore latest"
        )
    click.echo(f"Snapshot {snap.path.name} ({snap.size_mb:.0f}MB)")
    if offsite:
        target = manager.push_offsite(snap)
        click.echo(f"Offsite copy: {target}" if target
                   else "No backup.offsite_path configured.")


@main.command()
@click.option("--config", default="config/config.yaml", show_default=True)
@click.argument("snapshot", default="latest")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation.")
def restore(config: str, snapshot: str, yes: bool) -> None:
    """Restore a verified snapshot, keeping the current database alongside it.

    Pass a snapshot filename from 'job-search backup --list', or "latest".
    """
    from job_search.core import runcontrol
    from job_search.core.backup import DatabaseCorruptError

    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)

    holder = runcontrol.is_locked(cfg.execution.lock_file,
                                  cfg.execution.lock_stale_after_minutes)
    if holder is not None:
        raise click.ClickException(
            f"A run is in progress (pid {holder.pid}). Stop it first: job-search stop"
        )

    manager = _snapshot_manager(cfg)
    snaps = manager.list()
    if not snaps:
        raise click.ClickException("No snapshots to restore from.")
    chosen = snaps[0] if snapshot == "latest" else next(
        (s for s in snaps if s.path.name in (snapshot, f"{snapshot}.db")), None)
    if chosen is None:
        raise click.ClickException(f"No snapshot named {snapshot}")

    hours = chosen.age.total_seconds() / 3600
    click.echo(f"Restoring {chosen.path.name} ({chosen.size_mb:.0f}MB, {hours:.1f}h old).")
    click.echo("The current database is kept as jobs.db.replaced-<timestamp>.")
    if not yes:
        click.confirm("Proceed?", abort=True)

    try:
        path = manager.restore(chosen.path.name)
    except DatabaseCorruptError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"Restored to {path} — verified.")


# ---------------------------------------------------------------------------
# backfill-sizes: fetch the declared size band for already-scraped companies
# ---------------------------------------------------------------------------

@main.command("backfill-sizes")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--limit", default=None, type=int,
              help="Only do this many companies (largest first), then stop.")
@click.option("--max-runtime", type=float, default=None,
              help="Stop gracefully after this many hours. Re-run to continue.")
@click.option("--no-interactive", is_flag=True, default=False,
              help="Never open a browser. Required for scheduled runs.")
@click.option("--scheduled", is_flag=True, default=False,
              help="Honour the schedule pause and imply --no-interactive.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report how much work is outstanding without fetching anything.")
def backfill_sizes(config: str, limit: int | None, max_runtime: float | None,
                   no_interactive: bool, scheduled: bool, dry_run: bool) -> None:
    """Fetch the declared company size band for jobs scraped before it was captured.

    Walks distinct companies rather than jobs — one request repairs every job
    row that company owns. Safe to interrupt: a company already written is
    skipped next time, so re-running continues where it stopped.
    """
    from job_search.core import runcontrol
    from job_search.core.database import DatabaseManager
    from job_search.core.config import load_secrets
    from job_search.scraping.auth import get_session
    from job_search.scraping.company_backfill import CompanySizeBackfiller

    cfg = load_config(config)
    setup_logging(level=cfg.logging.level, log_file=cfg.logging.file)

    if scheduled:
        no_interactive = True
        if runcontrol.pause_state(cfg.schedule.pause_file) is not None:
            click.echo("Schedule is paused. Exiting.")
            return

    db = DatabaseManager(cfg.database.path)
    try:
        pending = db.get_companies_missing_size_band()
        if not pending:
            click.echo("Every company already has a size band. Nothing to do.")
            return

        jobs_affected = sum(c["job_count"] for c in pending)
        click.echo(f"{len(pending)} companies without a size band, "
                   f"covering {jobs_affected} job(s).")
        if dry_run:
            click.echo("Largest first:")
            for company in pending[:10]:
                click.echo(f"  {company['job_count']:>5} jobs  {company['company_name']}")
            return

        if not runcontrol.acquire_lock(
            cfg.execution.lock_file, origin="scheduled" if scheduled else "manual",
            stages="backfill-sizes",
            stale_after_minutes=cfg.execution.lock_stale_after_minutes,
        ):
            click.echo("Another run is in progress. Exiting.")
            return

        try:
            secrets = load_secrets()
            auth_cfg = cfg.auth
            session = get_session(
                email=secrets.linkedin_username,
                password=secrets.linkedin_password,
                session_file=auth_cfg.session_file,
                interactive=not no_interactive,
                interactive_timeout=auth_cfg.interactive_timeout,
            )
            if session is None:
                click.echo("No valid LinkedIn session. Run 'job-search login' first.")
                return

            runcontrol.clear_stop(cfg.execution.stop_file)
            _snapshot_before(cfg, "pre-backfill")
            backfiller = CompanySizeBackfiller(
                db, session,
                delay=cfg.search.rate_limits.delay_between_requests,
            )
            summary = backfiller.run(
                limit=limit,
                max_runtime_hours=max_runtime,
                should_stop=lambda: runcontrol.stop_requested(cfg.execution.stop_file),
            )
        finally:
            runcontrol.release_lock(cfg.execution.lock_file)
            runcontrol.clear_stop(cfg.execution.stop_file)

        click.echo(
            f"Wrote {summary['companies']} companies onto {summary['updated_jobs']} job(s). "
            f"No band: {summary['no_band']}  |  failed: {summary['failed']}  |  "
            f"still to do: {summary['remaining']}"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# purge-blocked: delete jobs from companies that are on the block list
# ---------------------------------------------------------------------------

@main.command("purge-blocked")
@click.option("--config", default="config/config.yaml", show_default=True)
@click.option("--dry-run", is_flag=True, default=False,
              help="List matching jobs without deleting them")
def purge_blocked(config: str, dry_run: bool) -> None:
    """Delete jobs whose company is on search.blocked_companies.

    DetailsWorker already discards blocked companies at scrape time, but jobs
    scraped *before* a company was added to the list stay in the database.
    Run this after extending the block list.
    """
    from collections import Counter

    from job_search.core.database import DatabaseManager

    cfg = load_config(config)
    db = DatabaseManager(cfg.database.path)
    try:
        blocked = cfg.search.blocked_companies
        rows = db.find_jobs_by_company_names(blocked)

        if not rows:
            click.echo(f"No jobs found for the {len(blocked)} blocked companies.")
            return

        by_company = Counter(company for _, company, _, _ in rows)
        click.echo(f"{len(rows)} job(s) from {len(by_company)} blocked company/companies:")
        for company, n in by_company.most_common():
            click.echo(f"  {n:>4}  {company}")

        applied = [r for r in rows if r[3] == "applied"]
        if applied:
            click.echo("")
            click.echo(f"WARNING: {len(applied)} of these are marked 'applied'. "
                       f"Deleting loses that history.")
            for job_id, company, title, _ in applied:
                click.echo(f"  {job_id}  {company}  |  {title}")

        if dry_run:
            click.echo("")
            click.echo("Dry run: nothing deleted. Re-run without --dry-run to delete.")
            return

        _snapshot_before(cfg, "pre-purge")
        for job_id, _, _, _ in rows:
            db.delete_job(job_id)
        click.echo("")
        click.echo(f"Deleted {len(rows)} job(s) and their screening/cover-letter rows.")
    finally:
        db.close()


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
    flask_app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
