"""
Flask web UI for reviewing selected jobs, reading cover letters, and
tracking application status.

Run with:  job-search web
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from job_search.core.config import Config, load_config
from job_search.core.database import APPLICATION_STATUSES, DatabaseManager

# Flask finds templates relative to this file's directory
app = Flask(__name__, template_folder="templates")
app.secret_key = "local-job-search-ui"


@app.template_filter("short_date")
def short_date_filter(value: str | None) -> str:
    """Format a datetime string (e.g. '2026-05-05 12:34:56') as '5 May'."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).split(".")[0])
        return f"{dt.day} {dt.strftime('%b')}"
    except (ValueError, TypeError):
        return str(value)[:10]

_db: DatabaseManager | None = None
_config: Config | None = None
_cl_mode: str = "auto"

# Runner state
_runner_thread: threading.Thread | None = None
_runner_coordinator = None


def get_db() -> DatabaseManager:
    if _db is None:
        raise RuntimeError("DatabaseManager not initialised")
    return _db


def get_cl_mode() -> str:
    return _cl_mode


def init_app(db: DatabaseManager, config: Config | None = None) -> Flask:
    global _db, _cl_mode, _config
    _db = db
    _config = config
    if config is not None:
        _cl_mode = config.cover_letter.mode
    return app


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    db = get_db()
    stats = db.get_stats()
    pipeline_stats = db.get_pipeline_stats()
    app_counts = db.get_application_counts()
    cl_mode = get_cl_mode()
    _APPROVAL_DAYS = 30
    _RECENT_DAYS = 7
    pending_approval = len(db.get_jobs_pending_cl_approval(days=_APPROVAL_DAYS)) if cl_mode == "user_approval" else 0
    recent_stats = db.get_recent_stats(days=_RECENT_DAYS)
    return render_template(
        "index.html", stats=stats, pipeline_stats=pipeline_stats,
        app_counts=app_counts, statuses=APPLICATION_STATUSES,
        cl_mode=cl_mode, pending_approval=pending_approval,
        approval_period_days=_APPROVAL_DAYS,
        recent_stats=recent_stats,
    )


_PAGE_SIZE = 50


def _get_page() -> int:
    try:
        return max(1, int(request.args.get("page", 1) or 1))
    except (ValueError, TypeError):
        return 1


@app.route("/jobs")
def jobs():
    db = get_db()
    status_filter  = request.args.get("status", "")
    sort_by        = request.args.get("sort", "created_at")
    sort_dir       = request.args.get("dir", "desc")
    search         = request.args.get("search", "").strip()
    remote_filter  = request.args.get("remote", "")   # "1" remote-only, "-1" hide-remote
    cl_filter      = request.args.get("cl_ready", "")
    date_from      = request.args.get("date_from", "")
    date_to        = request.args.get("date_to", "")
    page           = _get_page()
    offset         = (page - 1) * _PAGE_SIZE

    job_list, total = db.get_selected_jobs(
        sort_by=sort_by, sort_dir=sort_dir,
        search=search, status=status_filter,
        remote_filter=remote_filter,
        cl_ready=bool(cl_filter),
        date_from=date_from, date_to=date_to,
        limit=_PAGE_SIZE, offset=offset,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return render_template(
        "jobs.html", jobs=job_list, status_filter=status_filter,
        statuses=APPLICATION_STATUSES, show_all=False,
        current_sort=sort_by, current_dir=sort_dir,
        cl_mode=get_cl_mode(),
        search_query=search,
        remote_filter=remote_filter, cl_filter=cl_filter,
        date_from=date_from, date_to=date_to,
        page=page, total_pages=total_pages, total=total,
    )


@app.route("/jobs/all")
def jobs_all():
    db = get_db()
    status_filter  = request.args.get("status", "")
    sort_by        = request.args.get("sort", "created_at")
    sort_dir       = request.args.get("dir", "desc")
    search         = request.args.get("search", "").strip()
    remote_filter  = request.args.get("remote", "")
    cl_filter      = request.args.get("cl_ready", "")
    date_from      = request.args.get("date_from", "")
    date_to        = request.args.get("date_to", "")
    page           = _get_page()
    offset         = (page - 1) * _PAGE_SIZE

    job_list, total = db.get_all_jobs(
        sort_by=sort_by, sort_dir=sort_dir,
        search=search, status=status_filter,
        remote_filter=remote_filter,
        cl_ready=bool(cl_filter),
        date_from=date_from, date_to=date_to,
        limit=_PAGE_SIZE, offset=offset,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    return render_template(
        "jobs.html", jobs=job_list, status_filter=status_filter,
        statuses=APPLICATION_STATUSES, show_all=True,
        current_sort=sort_by, current_dir=sort_dir,
        cl_mode=get_cl_mode(),
        search_query=search,
        remote_filter=remote_filter, cl_filter=cl_filter,
        date_from=date_from, date_to=date_to,
        page=page, total_pages=total_pages, total=total,
    )


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    return render_template("job_detail.html", job=job, statuses=APPLICATION_STATUSES,
                           cl_mode=get_cl_mode())


@app.route("/jobs/<int:job_id>/status", methods=["POST"])
def update_status(job_id: int):
    db = get_db()
    status = request.form.get("status", "").strip()
    if status not in APPLICATION_STATUSES and status != "":
        abort(400)
    db.mark_application_status(job_id, status if status else None)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/stats")
def search_stats():
    db = get_db()
    _STATS_DAYS = 30
    combos = db.get_search_combo_stats(days=_STATS_DAYS)
    return render_template("stats.html", combos=combos, stats_period_days=_STATS_DAYS)


@app.route("/jobs/<int:job_id>/quick-apply", methods=["POST"])
def quick_apply(job_id: int):
    """Toggle applied status inline from the job list."""
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    new_status = None if job.application_status == "applied" else "applied"
    db.mark_application_status(job_id, new_status)
    return _redirect_to_list(request.form)


@app.route("/jobs/<int:job_id>/quick-skip", methods=["POST"])
def quick_skip(job_id: int):
    """Toggle skipped status inline from the job list."""
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    new_status = None if job.application_status == "skipped" else "skipped"
    db.mark_application_status(job_id, new_status)
    return _redirect_to_list(request.form)


@app.route("/jobs/<int:job_id>/cl-approve", methods=["POST"])
def cl_approve(job_id: int):
    """Mark job as approved for cover letter generation."""
    db = get_db()
    if db.get_selected_job(job_id) is None:
        abort(404)
    db.set_cl_approval(job_id, 1)
    return _redirect_back(request.form, job_id)


@app.route("/jobs/<int:job_id>/cl-reject", methods=["POST"])
def cl_reject(job_id: int):
    """Mark job as rejected for cover letter generation (user won't apply)."""
    db = get_db()
    if db.get_selected_job(job_id) is None:
        abort(404)
    db.set_cl_approval(job_id, 0)
    return _redirect_back(request.form, job_id)


@app.route("/jobs/<int:job_id>/cl-reset", methods=["POST"])
def cl_reset(job_id: int):
    """Clear the user CL approval decision."""
    db = get_db()
    if db.get_selected_job(job_id) is None:
        abort(404)
    db.set_cl_approval(job_id, None)
    return _redirect_back(request.form, job_id)


def _redirect_back(form, job_id: int):
    """Redirect to job detail or job list depending on the 'source' form field."""
    if form.get("source") == "detail":
        return redirect(url_for("job_detail", job_id=job_id))
    return _redirect_to_list(form)


def _redirect_to_list(form) -> "Response":
    status_filter = form.get("status_filter", "")
    show_all = form.get("show_all", "")
    if show_all:
        return redirect(url_for("jobs_all", status=status_filter) if status_filter else url_for("jobs_all"))
    if status_filter:
        return redirect(url_for("jobs", status=status_filter))
    return redirect(url_for("jobs"))


# ---------------------------------------------------------------------------
# Manual Job Import
# ---------------------------------------------------------------------------

# Matches a bare numeric ID or extracts the ID from a LinkedIn jobs URL.
# LinkedIn URL formats:
#   /jobs/view/4411863756/
#   /jobs/view/data-scientist-at-company-4411863756/
_JOB_ID_RE = re.compile(r"(?:linkedin\.com/jobs/view/[^/]*?[-/])?(\d{7,})")


def _parse_job_ids(raw: str) -> tuple[list[int], list[str]]:
    """Return (valid_ids, invalid_tokens) parsed from a free-form text input."""
    tokens = re.split(r"[\s,]+", raw.strip())
    valid, invalid = [], []
    seen = set()
    for token in tokens:
        if not token:
            continue
        m = _JOB_ID_RE.search(token)
        if m:
            job_id = int(m.group(1))
            if job_id not in seen:
                seen.add(job_id)
                valid.append(job_id)
        else:
            invalid.append(token)
    return valid, invalid


@app.route("/jobs/import", methods=["GET", "POST"])
def import_jobs():
    result = None
    if request.method == "POST":
        raw = request.form.get("job_ids", "")
        valid_ids, invalid_tokens = _parse_job_ids(raw)

        added, skipped = [], []
        db = get_db()
        for job_id in valid_ids:
            if db.job_exists(job_id):
                skipped.append(db.get_job_status(job_id))
            else:
                db.insert_job(job_id, keyword="manual", location_id="manual")
                # Push to the live details queue if the runner is running
                if _runner_coordinator is not None and _runner_thread is not None and _runner_thread.is_alive():
                    _runner_coordinator._details_queue.put(job_id)
                added.append(job_id)

        runner_live = _runner_thread is not None and _runner_thread.is_alive()
        result = {
            "added": added,
            "skipped": skipped,
            "invalid": invalid_tokens,
            "runner_live": runner_live,
        }

    return render_template("import_jobs.html", result=result)


# ---------------------------------------------------------------------------
# Runner UI Routes
# ---------------------------------------------------------------------------

@app.route("/runner")
def runner_dashboard():
    global _runner_thread, _runner_coordinator
    is_running = _runner_thread is not None and _runner_thread.is_alive()
    
    active_stages = []
    if is_running and _runner_coordinator is not None:
        active_stages = list(_runner_coordinator._stages)
    else:
        _runner_thread = None
        _runner_coordinator = None
        active_stages = ["search", "details", "screen", "cover-letter"]

    return render_template(
        "runner.html",
        is_running=is_running,
        active_stages=active_stages,
    )


@app.route("/runner/start", methods=["POST"])
def runner_start():
    global _runner_thread, _runner_coordinator, _config

    if _runner_thread is not None and _runner_thread.is_alive():
        return redirect(url_for("runner_dashboard"))

    if _config is None:
        abort(500, "Configuration not loaded")

    # Parse stages
    stages = request.form.getlist("stages")
    if not stages:
        from job_search.orchestration.coordinator import ALL_STAGES
        stages = list(ALL_STAGES)
        
    resume = request.form.get("resume") == "on"

    from job_search.orchestration.coordinator import JobSearchCoordinator
    _runner_coordinator = JobSearchCoordinator(_config, stages=set(stages))

    def run_pipeline():
        from job_search.utils.logging import setup_logging
        setup_logging(level=_config.logging.level, log_file=_config.logging.file)
        try:
            _runner_coordinator.start(resume=resume)
        finally:
            _runner_coordinator.cleanup()

    _runner_thread = threading.Thread(target=run_pipeline, name="runner-ui-thread", daemon=True)
    _runner_thread.start()

    return redirect(url_for("runner_dashboard"))


@app.route("/runner/stop", methods=["POST"])
def runner_stop():
    global _runner_coordinator
    if _runner_coordinator is not None:
        # Only signal shutdown — do NOT join threads here.
        # The background run_pipeline() thread owns cleanup() and will call it
        # when workers finish. Joining in a request handler would block the browser.
        _runner_coordinator._shutdown.request_shutdown()
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/status")
def runner_status():
    global _runner_thread
    is_running = _runner_thread is not None and _runner_thread.is_alive()
    return jsonify({"is_running": is_running})


@app.route("/runner/logs")
def runner_logs():
    global _config
    if _config is None or not _config.logging.file:
        return jsonify({"logs": "Log file not configured."})

    log_path = Path(_config.logging.file)
    if not log_path.exists():
        return jsonify({"logs": "Log file not found."})

    # Read the last N lines. Since log files can be large, we'll read from the end.
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            filesize = f.tell()
            block_size = 8192
            
            # Go back one block at a time
            lines = []
            pos = max(0, filesize - block_size)
            f.seek(pos, os.SEEK_SET)
            
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            
            # If the file is larger than our block, keep looking backwards to get up to 200 lines
            while len(lines) < 200 and pos > 0:
                pos = max(0, pos - block_size)
                f.seek(pos, os.SEEK_SET)
                data = f.read(block_size).decode("utf-8", errors="replace")
                lines = (data + lines[0]).splitlines() + lines[1:]

            last_lines = lines[-200:]
            return jsonify({"logs": "\n".join(last_lines)})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})
