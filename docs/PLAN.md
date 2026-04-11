# Plan: Auto-start web UI, auto-export, inline apply, SIGTERM, pipeline dashboard

## Context

The user wants the pipeline and web UI to run together as a single process from IntelliJ
(stop button → graceful shutdown → restart resumes from checkpoint). Cover letters should
appear in `data/export/` automatically without a manual CLI step. The web UI dashboard
should show the full pipeline funnel, and the job list should have an inline one-click
"Apply" button so status can be updated without navigating to the detail page.

---

## Implementation Order (10 steps, each depends only on prior steps)

### Step 1 — `src/job_search/core/config.py`

Add two new sub-models and wire them into `Config`:

```python
class WebUIConfig(BaseModel):
    auto_start: bool = True
    host: str = "127.0.0.1"
    port: int = 5000

class ExportConfig(BaseModel):
    output_dir: str = "data/export"
```

Add to `Config`:
```python
web: WebUIConfig = Field(default_factory=WebUIConfig)
export: ExportConfig = Field(default_factory=ExportConfig)
```

Both are fully optional — existing `config.yaml` files that omit these sections use defaults.

---

### Step 2 — `src/job_search/core/database.py`

Add `get_pipeline_stats()` after the existing `get_stats()` method. Returns a dict with:
`total_found`, `details_scraped`, `details_pending`, `details_error`,
`screened_ok`, `screened_error`, `screen_pass`, `screen_fail`,
`cl_generated`, `cl_pending`, `cl_error`.

All queries use existing indexed columns — no schema migration needed.

---

### Step 3 — `src/job_search/export/exporter.py`

**Add module-level lock** at the top of the file:
```python
import threading
_csv_lock = threading.Lock()
```

**Add `export_single_job(db, job_id, output_dir="data/export") -> str | None`:**
- Calls `db.get_selected_job(job_id)` to get full job data
- Writes `{company}_{title}_{job_id}.txt` using existing `_format_job_file()` and `_safe_name()`
- Under `_csv_lock`: reads existing `index.csv`, replaces the row if job_id already present,
  appends if new, rewrites file. Prevents race with concurrent cover letter completions.

**Update `export_cover_letters()`:** wrap the CSV write block with `with _csv_lock:` to prevent
a race with a live `export_single_job()` call running on the cover-letter thread.

---

### Step 4 — `src/job_search/ai/cover_letter.py`

Add `export_dir: str | None = None` as the last `__init__` param. Store as `self._export_dir`.

After line 138 (`logger.info("Cover letter generated...")`), add:
```python
if self._export_dir is not None:
    try:
        from job_search.export.exporter import export_single_job
        export_single_job(self._db, job_id, self._export_dir)
    except Exception as exc:
        logger.warning("Auto-export failed for job {}: {}", job_id, exc)
```

Deferred import avoids any circular import risk at module load time. Non-fatal — the cover
letter is already saved to DB even if file export fails.

---

### Step 5 — `src/job_search/web/app.py`

**Update `index` route** to also call `db.get_pipeline_stats()` and pass `pipeline_stats`
to the template.

**Add `quick-apply` route:**
```python
@app.route("/jobs/<int:job_id>/quick-apply", methods=["POST"])
def quick_apply(job_id: int):
    db = get_db()
    job = db.get_selected_job(job_id)
    if job is None:
        abort(404)
    # Toggle: applied → clear, anything else → applied
    new_status = None if job.application_status == "applied" else "applied"
    db.mark_application_status(job_id, new_status)
    status_filter = request.form.get("status_filter", "")
    return redirect(url_for("jobs", status=status_filter) if status_filter else url_for("jobs"))
```

---

### Step 6 — `src/job_search/web/templates/base.html`

Add `{% block head_extra %}{% endblock %}` inside `<head>`, after the closing `</style>` tag
(after line 22). This lets child templates inject per-page head content.

---

### Step 7 — `src/job_search/web/templates/index.html`

**Auto-refresh:** Add `{% block head_extra %}<meta http-equiv="refresh" content="30">{% endblock %}`
at the top of the template (so dashboard live-updates every 30s).

**Pipeline funnel section:** Insert between the existing stat cards and the application tracking
section. Render `pipeline_stats` as a horizontal funnel:
- Row of cards: Found → Details Scraped → Screened OK → Passed → CL Generated (with `→` arrows between)
- Second row of small badges: pending/error sub-counts for each stage

---

### Step 8 — `src/job_search/web/templates/jobs.html`

Replace the final `<td class="text-end">` in each table row with a version that has two
buttons side by side — an inline Apply toggle and the existing View button:

```html
<td class="text-end d-flex gap-1 justify-content-end">
  <form method="post" action="/jobs/{{ job.job_id }}/quick-apply" style="display:inline">
    <input type="hidden" name="status_filter" value="{{ status_filter }}">
    {% if job.application_status == "applied" %}
    <button type="submit" class="btn btn-sm btn-success" title="Undo applied">
      <i class="bi bi-check2 me-1"></i>Applied
    </button>
    {% else %}
    <button type="submit" class="btn btn-sm btn-outline-primary" title="Mark applied">
      <i class="bi bi-send me-1"></i>Apply
    </button>
    {% endif %}
  </form>
  <a href="/jobs/{{ job.job_id }}" class="btn btn-sm btn-outline-secondary">
    View <i class="bi bi-arrow-right ms-1"></i>
  </a>
</td>
```

The `status_filter` hidden field preserves the current filter on redirect back.
Button turns green when applied; clicking again clears it. Pure HTML form — no JS needed.

---

### Step 9 — `src/job_search/orchestration/coordinator.py`

**Add cleanup guard** at the top of `cleanup()` to make it idempotent:
```python
def cleanup(self) -> None:
    if self._shutdown.should_shutdown():
        return
    ...
```

**Add `_start_web_ui()` private method:**
```python
def _start_web_ui(self) -> None:
    from job_search.web.app import init_app
    flask_app = init_app(self._db)
    host, port = self._config.web.host, self._config.web.port

    def _run():
        try:
            flask_app.run(host=host, port=port, debug=False,
                          use_reloader=False, threaded=True)
        except OSError as exc:
            logger.warning("Web UI failed to start on port {}: {}", port, exc)

    threading.Thread(target=_run, name="web-ui", daemon=True).start()
    logger.info("Web UI started → http://{}:{}/", host, port)
```

**Do NOT add** the Flask thread to `self._threads` — Werkzeug has no clean shutdown API.
The daemon flag ensures it exits with the main process.

**In `_start_workers()`**, at the very top (before LinkedIn auth):
```python
if self._config.web.auto_start:
    self._start_web_ui()
```

**Pass `export_dir` to `CoverLetterWorker`** (coordinator.py lines 132–139):
```python
cl_worker = CoverLetterWorker(
    ...
    api_keys=api_keys,
    export_dir=self._config.export.output_dir,   # ADD THIS
)
```

---

### Step 10 — `src/job_search/__main__.py`

In the `run` command, after `coordinator = JobSearchCoordinator(cfg)`:

```python
# Print web UI URL if it will auto-start
if cfg.web.auto_start:
    click.echo(f"Web UI: http://{cfg.web.host}:{cfg.web.port}/")

# SIGTERM handler for IntelliJ stop button
import signal
try:
    signal.signal(signal.SIGTERM, lambda *_: coordinator.cleanup())
except (AttributeError, OSError):
    pass  # Not available on all Windows configurations
```

`--resume` defaults to `True` already — no change needed.

---

### Step 11 — `config/config.yaml.example` and user's `config/config.yaml`

Append at the end of both files:
```yaml
# Web UI (started automatically with the pipeline)
web:
  auto_start: true
  host: "127.0.0.1"
  port: 5000

# Export (cover letters written here automatically as generated)
export:
  output_dir: "data/export"
```

---

## Critical Files

| File | Change |
|------|--------|
| `src/job_search/core/config.py` | Add `WebUIConfig`, `ExportConfig`, wire into `Config` |
| `src/job_search/core/database.py` | Add `get_pipeline_stats()` |
| `src/job_search/export/exporter.py` | Add `_csv_lock`, `export_single_job()`, lock bulk export |
| `src/job_search/ai/cover_letter.py` | Add `export_dir` param, call `export_single_job` after save |
| `src/job_search/web/app.py` | Add `quick-apply` route, pass `pipeline_stats` to index |
| `src/job_search/web/templates/base.html` | Add `{% block head_extra %}` |
| `src/job_search/web/templates/index.html` | Auto-refresh meta, pipeline funnel section |
| `src/job_search/web/templates/jobs.html` | Inline Apply/Applied toggle button per row |
| `src/job_search/orchestration/coordinator.py` | `_start_web_ui()`, cleanup guard, `export_dir` to CL worker |
| `src/job_search/__main__.py` | SIGTERM handler, print web URL |
| `config/config.yaml.example` + user's `config.yaml` | Add `web:` and `export:` sections |

---

## Verification

1. `uv run pytest tests/ -v` — all 71 tests must still pass (no breaking changes to DB or config)
2. `uv run job-search run` — confirm log output shows "Web UI started → http://127.0.0.1:5000/"
3. Open browser to `http://127.0.0.1:5000/` — dashboard shows pipeline funnel with counts
4. Dashboard auto-refreshes every 30 seconds (verify via browser network tab or just wait)
5. On a job with a generated cover letter, check `data/export/` for the `.txt` file
6. In the jobs list, click "Apply" — button turns green, status badge updates, no page navigation required
7. Press IntelliJ stop button (SIGTERM) — pipeline logs "Shutdown complete", DB closed cleanly
8. Re-run with `job-search run` — log shows "Resumed: X pending details, Y pending screening, Z pending cover letters"
9. Run standalone `job-search web` after pipeline stops — works independently on same DB
