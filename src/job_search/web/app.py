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
    app_counts = db.get_application_counts()
    return render_template("index.html", stats=stats, app_counts=app_counts,
                           statuses=APPLICATION_STATUSES)


@app.route("/jobs")
def jobs():
    db = get_db()
    status_filter = request.args.get("status", "")
    all_jobs = db.get_selected_jobs()
    if status_filter:
        if status_filter == "pending":
            all_jobs = [j for j in all_jobs if not j.application_status]
        else:
            all_jobs = [j for j in all_jobs if j.application_status == status_filter]
    return render_template("jobs.html", jobs=all_jobs, status_filter=status_filter,
                           statuses=APPLICATION_STATUSES)


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
    if status:
        db.mark_application_status(job_id, status)
    else:
        # Clear status
        db.mark_application_status(job_id, None)
    return redirect(url_for("job_detail", job_id=job_id))
