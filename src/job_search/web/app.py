"""
Flask web UI for reviewing selected jobs, reading cover letters, and
tracking application status.

Run with:  job-search web
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template, request,
    send_file, url_for,
)

from job_search.core.config import Config, load_config
from job_search.core.database import (
    APPLICATION_STATUSES,
    ARCHETYPE_LABELS,
    DatabaseManager,
)
from job_search.utils.formatting import clean_cover_letter_text

# Flask finds templates relative to this file's directory
app = Flask(__name__, template_folder="templates")
app.secret_key = "local-job-search-ui"
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.template_filter("clean_cl")
def clean_cl_filter(value: str | None) -> str:
    """Clean cover letter text by normalizing line breaks for MS Word compatibility."""
    return clean_cover_letter_text(value)


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


@app.template_filter("thousands")
def thousands_filter(value) -> str:
    """Format an integer with thousands separators (11350 -> '11,350')."""
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value) if value is not None else ""


@app.template_filter("industry")
def industry_filter(value: str | None) -> str:
    """Render the primary industry from the stored JSON array of industries."""
    if not value:
        return ""
    try:
        arr = json.loads(value)
        if isinstance(arr, list) and arr:
            return str(arr[0])
    except (ValueError, TypeError):
        pass
    return str(value).strip('[]"')

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


def get_blocked_companies() -> list[str]:
    """Return the blocked companies list from config (empty list if config not loaded)."""
    if _config is None:
        return []
    return list(_config.search.blocked_companies)


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
    days_param = request.args.get("days", "all").strip().lower()
    days_map = {"24h": 1, "14d": 14, "30d": 30, "90d": 90, "all": None}
    days_val = days_map.get(days_param, None)

    stats = db.get_stats()
    pipeline_stats = db.get_pipeline_stats(days=days_val, cl_mode=get_cl_mode())
    app_counts = db.get_application_counts(days=days_val)
    cl_mode = get_cl_mode()
    _APPROVAL_DAYS = 30
    _RECENT_DAYS = 7
    pending_approval = len(db.get_jobs_pending_cl_approval(days=_APPROVAL_DAYS)) if cl_mode == "user_approval" else 0
    pending_cl_gen = len(db.get_jobs_pending_cover_letter(mode=cl_mode))
    recent_stats = db.get_recent_stats(days=_RECENT_DAYS)
    is_runner_active = _runner_thread is not None and _runner_thread.is_alive()
    return render_template(
        "index.html", stats=stats, pipeline_stats=pipeline_stats,
        app_counts=app_counts, statuses=APPLICATION_STATUSES,
        cl_mode=cl_mode, pending_approval=pending_approval,
        pending_cl_gen=pending_cl_gen,
        approval_period_days=_APPROVAL_DAYS,
        recent_stats=recent_stats,
        is_runner_active=is_runner_active,
        active_days=days_param,
        archetype_counts=db.get_archetype_counts(selected_only=True),
    )


_PAGE_SIZE = 25


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
    exclude_companies = request.args.getlist("exc")
    include_companies = request.args.getlist("inc")
    keyword_filter = request.args.get("kw", "").strip()
    german_filter  = request.args.get("german", "").strip()
    apply_type     = request.args.get("apply_type", "").strip()
    archetype_filter = request.args.get("archetype", "").strip()
    prefilter_filter = request.args.get("prefiltered", "").strip()
    size_filter    = request.args.get("size", "").strip()
    min_match_param = request.args.get("min_match", "").strip()
    try:
        min_match_val = float(min_match_param) if min_match_param else None
    except ValueError:
        min_match_val = None
    page           = _get_page()
    offset         = (page - 1) * _PAGE_SIZE

    all_excluded = list(dict.fromkeys(exclude_companies))
    all_included = list(dict.fromkeys(include_companies))

    company_counts = db.get_company_counts(
        selected_only=True,
        status=status_filter, remote_filter=remote_filter,
        date_from=date_from, date_to=date_to,
        search=search, cl_ready=bool(cl_filter),
        exclude_companies=all_excluded or None,
        include_companies=all_included or None,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_val,
        apply_type=apply_type,
    )
    job_list, total = db.get_selected_jobs(
        sort_by=sort_by, sort_dir=sort_dir,
        search=search, status=status_filter,
        remote_filter=remote_filter,
        cl_ready=bool(cl_filter),
        date_from=date_from, date_to=date_to,
        exclude_companies=all_excluded or None,
        include_companies=all_included or None,
        limit=_PAGE_SIZE, offset=offset,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_val,
        apply_type=apply_type,
        archetype_filter=archetype_filter,
        prefilter_filter=prefilter_filter,
        size_filter=size_filter,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    distinct_keywords = db.get_distinct_keywords()

    return render_template(
        "jobs.html", jobs=job_list, status_filter=status_filter,
        statuses=APPLICATION_STATUSES, show_all=False,
        current_sort=sort_by, current_dir=sort_dir,
        cl_mode=get_cl_mode(),
        search_query=search,
        remote_filter=remote_filter, cl_filter=cl_filter,
        date_from=date_from, date_to=date_to,
        company_counts=company_counts,
        exclude_companies=exclude_companies,
        include_companies=include_companies,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_param,
        apply_type=apply_type,
        archetype_filter=archetype_filter,
        archetype_counts=db.get_archetype_counts(selected_only=True),
        archetype_labels=ARCHETYPE_LABELS,
        prefilter_filter=prefilter_filter,
        size_filter=size_filter,
        prefilter_counts=db.get_prefilter_counts(),
        distinct_keywords=distinct_keywords,
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
    exclude_companies = request.args.getlist("exc")
    include_companies = request.args.getlist("inc")
    keyword_filter = request.args.get("kw", "").strip()
    german_filter  = request.args.get("german", "").strip()
    apply_type     = request.args.get("apply_type", "").strip()
    archetype_filter = request.args.get("archetype", "").strip()
    prefilter_filter = request.args.get("prefiltered", "").strip()
    size_filter    = request.args.get("size", "").strip()
    min_match_param = request.args.get("min_match", "").strip()
    try:
        min_match_val = float(min_match_param) if min_match_param else None
    except ValueError:
        min_match_val = None
    page           = _get_page()
    offset         = (page - 1) * _PAGE_SIZE

    all_excluded = list(dict.fromkeys(exclude_companies))
    all_included = list(dict.fromkeys(include_companies))

    company_counts = db.get_company_counts(
        selected_only=False,
        status=status_filter, remote_filter=remote_filter,
        date_from=date_from, date_to=date_to,
        search=search, cl_ready=bool(cl_filter),
        exclude_companies=all_excluded or None,
        include_companies=all_included or None,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_val,
        apply_type=apply_type,
        limit=200,
    )
    job_list, total = db.get_all_jobs(
        sort_by=sort_by, sort_dir=sort_dir,
        search=search, status=status_filter,
        remote_filter=remote_filter,
        cl_ready=bool(cl_filter),
        date_from=date_from, date_to=date_to,
        exclude_companies=all_excluded or None,
        include_companies=all_included or None,
        limit=_PAGE_SIZE, offset=offset,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_val,
        apply_type=apply_type,
        archetype_filter=archetype_filter,
        prefilter_filter=prefilter_filter,
        size_filter=size_filter,
    )
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    distinct_keywords = db.get_distinct_keywords()

    return render_template(
        "jobs.html", jobs=job_list, status_filter=status_filter,
        statuses=APPLICATION_STATUSES, show_all=True,
        current_sort=sort_by, current_dir=sort_dir,
        cl_mode=get_cl_mode(),
        search_query=search,
        remote_filter=remote_filter, cl_filter=cl_filter,
        date_from=date_from, date_to=date_to,
        company_counts=company_counts,
        exclude_companies=exclude_companies,
        include_companies=include_companies,
        keyword_filter=keyword_filter,
        german_filter=german_filter,
        min_match=min_match_param,
        apply_type=apply_type,
        archetype_filter=archetype_filter,
        archetype_counts=db.get_archetype_counts(selected_only=True),
        archetype_labels=ARCHETYPE_LABELS,
        prefilter_filter=prefilter_filter,
        size_filter=size_filter,
        prefilter_counts=db.get_prefilter_counts(),
        distinct_keywords=distinct_keywords,
        page=page, total_pages=total_pages, total=total,
    )


@app.route("/jobs/<int:job_id>")
def job_detail(job_id: int):
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    prev_job_id, next_job_id = db.get_adjacent_job_ids(job_id)
    return render_template("job_detail.html", job=job, statuses=APPLICATION_STATUSES,
                           archetype_labels=ARCHETYPE_LABELS,
                           cl_mode=get_cl_mode(),
                           prev_job_id=prev_job_id,
                           next_job_id=next_job_id)


@app.route("/jobs/<int:job_id>/status", methods=["POST"])
def update_status(job_id: int):
    db = get_db()
    status = request.form.get("status", "").strip()
    if status not in APPLICATION_STATUSES and status != "":
        abort(400)
    db.mark_application_status(job_id, status if status else None)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/clean", methods=["POST"])
def clean_jobs():
    global _runner_thread, _runner_coordinator, _config
    if _runner_thread is None or not _runner_thread.is_alive():
        if _config is not None:
            from job_search.orchestration.coordinator import JobSearchCoordinator
            _runner_coordinator = JobSearchCoordinator(_config, stages={"clean"})

            def run_pipeline():
                from job_search.utils.logging import setup_logging
                setup_logging(level=_config.logging.level, log_file=_config.logging.file)
                try:
                    _runner_coordinator.start(resume=True)
                finally:
                    _runner_coordinator.cleanup()

            _runner_thread = threading.Thread(target=run_pipeline, name="runner-ui-thread", daemon=True)
            _runner_thread.start()

    return redirect(url_for("runner_dashboard"))


@app.route("/jobs/<int:job_id>/notes", methods=["POST"])
def update_notes(job_id: int):
    db = get_db()
    notes = request.form.get("notes", "").strip()
    db.update_user_notes(job_id, notes if notes else None)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/cover-letter/update", methods=["POST"])
def update_cover_letter(job_id: int):
    db = get_db()
    cl_text = request.form.get("cover_letter_text", "").strip()
    if db.get_selected_job(job_id) is None:
        abort(404)
    db.save_cover_letter(job_id, cl_text, model="manual-edit", api_key_index=0)
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/cover-letter/delete", methods=["POST"])
def delete_cover_letter(job_id: int):
    db = get_db()
    if db.get_selected_job(job_id) is None:
        abort(404)

    # 1. Clear database cover letter record and update status
    db.delete_cover_letter_record(job_id)

    # 2. Delete export files (.txt, .pdf) and update index.csv
    from job_search.export.exporter import delete_cover_letter_export
    project_root = Path(app.root_path).parents[2]
    delete_cover_letter_export(db, job_id, output_dir=str(project_root / "data" / "export"))

    flash(f"Cover letter deleted for job #{job_id}. Status updated in DB.", "success")
    return _redirect_back(request.form, job_id)


@app.route("/jobs/<int:job_id>/cover-letter/regenerate", methods=["POST"])
def regenerate_cover_letter(job_id: int):
    db = get_db()
    if db.get_selected_job(job_id) is None:
        abort(404)

    # 1. Clear existing cover letter record & export files
    db.prepare_cover_letter_regeneration(job_id)
    from job_search.export.exporter import delete_cover_letter_export
    project_root = Path(app.root_path).parents[2]
    delete_cover_letter_export(db, job_id, output_dir=str(project_root / "data" / "export"))

    # 2. Push job into live runner queue if active
    global _runner_coordinator, _runner_thread
    enqueued = False
    if _runner_coordinator is not None and _runner_thread is not None and _runner_thread.is_alive():
        try:
            _runner_coordinator._cover_letter_queue.put(job_id)
            enqueued = True
        except Exception as exc:
            from loguru import logger
            logger.warning("Could not push job {} to live queue: {}", job_id, exc)

    if enqueued:
        flash(f"Cover letter reset for job #{job_id} and pushed to live generation queue!", "success")
    else:
        flash(f"Cover letter reset for job #{job_id} and marked approved for generation. Start the runner to generate.", "info")

    return _redirect_back(request.form, job_id)



@app.route("/jobs/<int:job_id>/cover-letter/pdf", methods=["GET", "POST"])
def download_cover_letter_pdf(job_id: int):
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None and request.method == "GET":
        abort(404)
    
    # Read live unsaved inputs from form / json / args with robust key fallbacks
    override_title = (
        request.form.get("job_title")
        or request.form.get("pdf_job_title")
        or request.args.get("job_title")
        or request.args.get("pdf_job_title")
    )
    override_company = (
        request.form.get("company_name")
        or request.form.get("pdf_company_name")
        or request.args.get("company_name")
        or request.args.get("pdf_company_name")
    )
    override_cl_text = (
        request.form.get("cover_letter_text")
        or request.args.get("cover_letter_text")
    )

    # Workaround / Auto-save live draft to DB so edits are preserved persistently
    if override_cl_text and override_cl_text.strip():
        try:
            db.save_cover_letter(job_id, override_cl_text.strip(), model="live-draft", api_key_index=0)
        except Exception as e:
            from loguru import logger
            logger.warning("Could not auto-save cover letter live draft: {}", e)

    if (override_title and override_title.strip()) or (override_company and override_company.strip()):
        updates = {}
        if override_title and override_title.strip():
            updates["title"] = override_title.strip()
        if override_company and override_company.strip():
            updates["company_name"] = override_company.strip()
        try:
            db.update_job_details(job_id, updates)
        except Exception as e:
            from loguru import logger
            logger.warning("Could not auto-save job title/company live draft: {}", e)

    try:
        from job_search.export.latex_exporter import generate_cover_letter_pdf
        # Project root directory containing config/ and data/
        project_root = Path(app.root_path).parents[2]
        pdf_path = generate_cover_letter_pdf(
            job_id,
            db,
            project_root,
            override_title=override_title,
            override_company=override_company,
            override_cl_text=override_cl_text,
        )
        
        rel_path = str(pdf_path.resolve())

        # Return JSON confirmation for POST/AJAX requests instead of forcing file download
        if request.method == "POST" or request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            return jsonify({
                "success": True,
                "pdf_path": str(pdf_path.resolve()),
                "relative_path": rel_path,
                "filename": pdf_path.name,
                "message": f"PDF generated successfully at {rel_path}",
            })

        return send_file(pdf_path, as_attachment=False)
    except Exception as exc:
        from loguru import logger
        logger.error("Failed to generate PDF cover letter for job {}: {}", job_id, exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/jobs/<int:job_id>/cover-letter/prompt")
def cover_letter_prompt(job_id: int):
    """Show the exact cover letter prompt for a job, ready to paste elsewhere.

    Useful for trying the same prompt against a different model without
    re-implementing the archetype selection or the CV rendering.
    """
    from job_search.ai.prompt_manager import PromptManager

    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)

    prompts = PromptManager()
    system, user = prompts.format_cover_letter_prompt(
        job_title=job.title or "",
        company_name=job.company_name,
        job_location=job.formattedLocation,
        job_description=job.description,
        archetype=job.archetype,
    )
    combined = (
        "===== SYSTEM PROMPT =====\n\n" + system
        + "\n\n===== USER PROMPT =====\n\n" + user + "\n"
    )

    if request.args.get("download"):
        safe = re.sub(r"[^\w\-]", "_", (job.company_name or "job"))[:40]
        return Response(
            combined,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="cover_letter_prompt_{safe}_{job_id}.txt"'},
        )

    return render_template(
        "prompt_export.html", job=job, system_prompt=system,
        user_prompt=user, combined=combined,
        archetype_labels=ARCHETYPE_LABELS,
    )


@app.route("/stats")
def search_stats():
    db = get_db()
    days_param = request.args.get("days", 30, type=int)
    days = days_param if days_param in (7, 30, 90, 0) else 30
    combos = db.get_search_combo_stats(days=days if days > 0 else None)
    return render_template("stats.html", combos=combos, stats_period_days=days,
                           archetype_counts=db.get_archetype_counts(),
                           archetype_labels=ARCHETYPE_LABELS)


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


@app.route("/jobs/batch-status", methods=["POST"])
def batch_update_status():
    db = get_db()
    status = request.form.get("status", "").strip()
    raw_ids = request.form.getlist("job_ids")
    if status not in APPLICATION_STATUSES and status != "":
        abort(400)
    job_ids = [int(i) for i in raw_ids if str(i).isdigit()]
    if job_ids:
        db.mark_application_status_batch(job_ids, status if status else None)
    return _redirect_to_list(request.form)


@app.route("/jobs/batch-cl-approve", methods=["POST"])
def batch_cl_approve():
    db = get_db()
    action = request.form.get("action", "").strip()
    raw_ids = request.form.getlist("job_ids")
    job_ids = [int(i) for i in raw_ids if str(i).isdigit()]
    if job_ids:
        if action == "approve":
            db.set_cl_approval_batch(job_ids, 1)
        elif action == "reject":
            db.set_cl_approval_batch(job_ids, 0)
        elif action == "reset":
            db.set_cl_approval_batch(job_ids, None)
    return _redirect_to_list(request.form)


def _redirect_back(form, job_id: int):
    """Redirect to job detail or job list depending on the 'source' form field."""
    if form.get("source") == "detail":
        return redirect(url_for("job_detail", job_id=job_id))
    return _redirect_to_list(form)


def _redirect_to_list(form) -> "Response":
    referrer = request.referrer
    if referrer:
        parsed = urlparse(referrer)
        if not parsed.netloc or parsed.netloc == request.host:
            return redirect(referrer)

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

@app.route("/prefiltered")
def prefiltered():
    """Review what the deterministic prefilters caught, rule by rule.

    Separate from the jobs list because title-stage rejections are never
    scraped, and the jobs list requires scraped = 1. Filtering that list by a
    title rule returned an empty table, which read as "nothing matched" rather
    than "these are not in this table".
    """
    db = get_db()
    rules = db.get_prefilter_counts()
    reason = request.args.get("reason", "").strip()

    match = next((r for r in rules if r["reason"] == reason), None)
    jobs = db.get_prefiltered_jobs(reason) if match else []

    return render_template(
        "prefiltered.html",
        rules=rules,
        total=sum(r["count"] for r in rules),
        selected_reason=match["reason"] if match else "",
        selected_count=match["count"] if match else 0,
        selected_stage=match["stage"] if match else "",
        jobs=jobs,
    )


@app.route("/runner")
def runner_dashboard():
    global _runner_thread, _runner_coordinator
    from job_search.core import runcontrol
    from job_search.core.session_store import load_session, session_saved_at

    db = get_db()
    in_process = _runner_thread is not None and _runner_thread.is_alive()

    active_stages = []
    if in_process and _runner_coordinator is not None:
        active_stages = list(_runner_coordinator._stages)
    else:
        _runner_thread = None
        _runner_coordinator = None
        active_stages = ["search", "details", "screen", "cover-letter"]

    # The lock is the only thing that can see a scheduled run in another process.
    lock = runcontrol.is_locked(
        _config.execution.lock_file, _config.execution.lock_stale_after_minutes
    ) if _config else None
    is_running = in_process or lock is not None

    session_saved = session_saved_at(_config.auth.session_file) if _config else None
    return render_template(
        "runner.html",
        is_running=is_running,
        active_stages=active_stages,
        lock=lock,
        stopping=runcontrol.stop_requested(_config.execution.stop_file) if _config else False,
        pause_remaining=runcontrol.pause_remaining(_config.schedule.pause_file) if _config else None,
        session_saved_at=session_saved,
        open_batches=db.get_open_batch_jobs(),
        recent_batches=db.get_recent_batch_jobs(limit=8),
    )


@app.route("/runner/stop", methods=["POST"])
def runner_stop():
    """Ask the running pipeline to stop, whichever process owns it.

    Two mechanisms, because a run may live in this process (started from the
    web UI) or in a separate scheduled process. Signalling the in-process
    coordinator is immediate; the stop file is what a scheduled run sees.
    Threads are deliberately not joined here, the background run_pipeline()
    thread owns cleanup and joining would block the browser.
    """
    global _runner_coordinator
    from job_search.core import runcontrol

    if _runner_coordinator is not None:
        _runner_coordinator._shutdown.request_shutdown()

    runcontrol.request_stop(_config.execution.stop_file, reason="requested from web UI")
    flash("Stop requested. The run will finish its current item and exit.", "warning")
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/force-stop", methods=["POST"])
def runner_force_stop():
    """Terminate the run by pid and clear the lock. Blunt, but it is your machine."""
    import signal

    from job_search.core import runcontrol

    lock = runcontrol.read_lock(_config.execution.lock_file)
    if lock is None:
        flash("Nothing is running.", "info")
        return redirect(url_for("runner_dashboard"))
    try:
        os.kill(lock.pid, signal.SIGTERM)
        flash(f"Sent SIGTERM to pid {lock.pid}.", "warning")
    except OSError as exc:
        flash(f"Could not terminate pid {lock.pid}: {exc}", "danger")
    runcontrol.release_lock(_config.execution.lock_file)
    runcontrol.clear_stop(_config.execution.stop_file)
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/pause", methods=["POST"])
def runner_pause():
    from job_search.core import runcontrol

    preset = request.form.get("preset", _config.schedule.default_pause)
    resume_at = runcontrol.pause_schedule(
        _config.schedule.pause_file, preset=preset,
        morning_hour=_config.schedule.morning_resume_hour,
    )
    flash(
        f"Scheduled runs paused until {resume_at:%a %H:%M}." if resume_at
        else "Scheduled runs paused indefinitely. Remember to resume them.",
        "warning" if resume_at else "danger",
    )
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/resume", methods=["POST"])
def runner_resume():
    from job_search.core import runcontrol

    runcontrol.resume_schedule(_config.schedule.pause_file)
    flash("Scheduled runs resumed.", "success")
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/batch/collect", methods=["POST"])
def runner_collect_batches():
    """Poll every open batch now and write back whatever has finished.

    Safe to press at any time, including mid-run: a result is only written if
    the job still points at the batch it came from, so nothing here can
    overwrite a fresher answer.
    """
    if _config is None:
        abort(500, "Configuration not loaded")

    from job_search.ai.batch_screener import BatchScreener
    from job_search.core.config import load_secrets

    api_keys = load_secrets().gemini_api_keys
    if not api_keys:
        flash("No Gemini API key configured, cannot reach the Batch API.", "danger")
        return redirect(url_for("runner_dashboard"))

    if not get_db().get_open_batch_jobs():
        flash("No open batches to collect.", "info")
        return redirect(url_for("runner_dashboard"))

    try:
        screener = BatchScreener(_config, get_db(), api_key=api_keys[0])
        summary = screener.collect_all(
            stale_after_hours=_config.screening.batch_stale_after_hours
        )
    except Exception as exc:
        flash(f"Could not collect batches: {exc}", "danger")
        return redirect(url_for("runner_dashboard"))

    if summary["collected"]:
        flash(
            f"Collected {summary['collected']} screening result(s) from "
            f"{summary['checked']} batch(es).",
            "success",
        )
    elif summary["still_running"]:
        flash(
            f"{summary['still_running']} batch(es) are still running at the "
            f"provider. Nothing to write back yet.",
            "info",
        )
    else:
        flash(f"Checked {summary['checked']} batch(es), nothing new.", "info")
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/batch/<int:batch_id>/abandon", methods=["POST"])
def runner_abandon_batch(batch_id: int):
    """Release a batch's jobs so they can be screened immediately."""
    released = get_db().abandon_batch_job(batch_id)
    flash(
        f"Batch {batch_id} abandoned, {released} job(s) released. "
        f"Those requests will be billed twice if the batch still completes.",
        "warning",
    )
    return redirect(url_for("runner_dashboard"))


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


@app.route("/runner/clear-errors", methods=["POST"])
def runner_clear_errors():
    db = get_db()
    db.reset_all_pipeline_errors()
    return redirect(url_for("runner_dashboard"))


@app.route("/runner/status")
def runner_status():
    global _runner_thread
    from job_search.core import runcontrol

    # Must match runner_dashboard's definition exactly: a scheduled run in
    # another process is only visible through the lock. If this checked the
    # in-process thread alone, it would report "not running" during a scheduled
    # run while the page was rendered "running", and the client would reload the
    # page on every poll trying to reconcile the two.
    in_process = _runner_thread is not None and _runner_thread.is_alive()
    lock = runcontrol.is_locked(
        _config.execution.lock_file, _config.execution.lock_stale_after_minutes
    ) if _config else None
    is_running = in_process or lock is not None

    db = get_db()
    pipeline_stats = db.get_pipeline_stats(cl_mode=get_cl_mode())
    return jsonify({
        "is_running": is_running,
        "pipeline_stats": pipeline_stats,
    })


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
