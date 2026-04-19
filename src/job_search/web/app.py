"""
Flask web UI for reviewing selected jobs, reading cover letters, and
tracking application status.

Run with:  job-search web
"""
from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for

from job_search.core.config import load_config
from job_search.core.database import APPLICATION_STATUSES, DatabaseManager

# Flask finds templates relative to this file's directory
app = Flask(__name__, template_folder="templates")
app.secret_key = "local-job-search-ui"

_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    if _db is None:
        raise RuntimeError("DatabaseManager not initialised")
    return _db


def init_app(db: DatabaseManager) -> Flask:
    global _db
    _db = db
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
    return render_template("index.html", stats=stats, pipeline_stats=pipeline_stats,
                           app_counts=app_counts, statuses=APPLICATION_STATUSES)


@app.route("/jobs")
def jobs():
    db = get_db()
    status_filter = request.args.get("status", "")
    sort_by = request.args.get("sort", "cv_match_score")
    sort_dir = request.args.get("dir", "desc")
    job_list = db.get_selected_jobs(sort_by=sort_by, sort_dir=sort_dir)
    if status_filter:
        if status_filter == "pending":
            job_list = [j for j in job_list if not j.application_status]
        else:
            job_list = [j for j in job_list if j.application_status == status_filter]
    return render_template(
        "jobs.html", jobs=job_list, status_filter=status_filter,
        statuses=APPLICATION_STATUSES, show_all=False,
        current_sort=sort_by, current_dir=sort_dir,
    )


@app.route("/jobs/all")
def jobs_all():
    db = get_db()
    sort_by = request.args.get("sort", "listedAt")
    sort_dir = request.args.get("dir", "desc")
    job_list = db.get_all_jobs(sort_by=sort_by, sort_dir=sort_dir)
    return render_template(
        "jobs.html", jobs=job_list, status_filter="",
        statuses=APPLICATION_STATUSES, show_all=True,
        current_sort=sort_by, current_dir=sort_dir,
    )


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    return render_template("job_detail.html", job=job, statuses=APPLICATION_STATUSES)


@app.route("/jobs/<int:job_id>/status", methods=["POST"])
def update_status(job_id: int):
    db = get_db()
    status = request.form.get("status", "").strip()
    if status not in APPLICATION_STATUSES and status != "":
        abort(400)
    db.mark_application_status(job_id, status if status else None)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/quick-apply", methods=["POST"])
def quick_apply(job_id: int):
    """Toggle applied status inline from the job list."""
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    new_status = None if job.application_status == "applied" else "applied"
    db.mark_application_status(job_id, new_status)
    status_filter = request.form.get("status_filter", "")
    show_all = request.form.get("show_all", "")
    if show_all:
        return redirect(url_for("jobs_all"))
    if status_filter:
        return redirect(url_for("jobs", status=status_filter))
    return redirect(url_for("jobs"))
