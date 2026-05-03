# AI-Assisted Job Search Tool

**Title:** AI-Assisted Job Search Pipeline

**Summary:** End-to-end Python automation tool that scrapes LinkedIn job postings, screens them against a structured CV using the Gemini API, and generates tailored cover letters — cutting manual review time from hours to minutes.

---

## Purpose

Built to automate the most time-consuming parts of a job search: filtering hundreds of LinkedIn postings down to a shortlist of genuinely relevant roles and generating a personalised cover letter for each one — entirely from a local machine with no paid SaaS dependencies.

---

## Technical Stack

| Layer | Technology |
|-------|-----------|
| Scraping | Python · Selenium (LinkedIn auth) · requests |
| AI Screening | Google Gemini API (default, multi-worker) · llama-cpp-python / GGUF (optional local) |
| Cover Letter | Google Gemini API · google-genai SDK · Google Search grounding |
| Concurrency | Python threading · queue-based pipeline |
| Storage | SQLite · custom schema (87 mapped fields, 6 tables) |
| Config | Pydantic v2 · pydantic-settings · YAML |
| Web UI | Flask · Jinja2 templates |
| CLI | Click |
| Testing | pytest · 71 unit tests |

---

## Architecture Highlights

- **Queue-based pipeline**: four decoupled worker stages (search → details → screening → cover letter) communicate through `queue.Queue` instances. Each stage scales independently; graceful shutdown is coordinated via a shared `ShutdownCoordinator` event.

- **Dual screening backends**: a `GeminiScreeningWorker` (Gemini API, N concurrent threads) and a `ScreeningWorker` (local GGUF, single GPU-bound thread) are selected at runtime via a single config key (`screening.backend`). Both share module-level JSON parsing and criteria evaluation helpers.

- **API key rotation**: `GeminiAPIRotator` implements thread-safe round-robin key selection with a rolling 60-second request window, per-key exponential backoff (60s → 600s), and blocking wait when all keys are rate-limited. Used independently by both the screening and cover letter stages.

- **Resume on restart**: pipeline state (pending job IDs at each stage) is persisted to SQLite and reloaded at startup with `--resume`, so interrupted runs pick up exactly where they left off.

- **Pydantic config system**: a single `Config` model with nested sub-models covers all tuneable parameters. Secrets are loaded separately via `pydantic-settings` from a `.env` file, keeping credentials out of YAML.

---

## Key Features

- Scrapes up to 500 LinkedIn results per keyword/location with automatic pagination and rate limiting
- Scores each job against a structured CV (YAML) on a 0–1 match scale; filters by minimum match score and German language requirement level
- Generates personalised cover letters with optional Google Search grounding so the model can research the target company in real time
- Flask web dashboard: sortable job table, full cover letter viewer with copy button, one-click application status tracking (applied / interviewing / offered / rejected)
- Export: per-job text files (job header + cover letter) and a CSV index ready for application tracking
- 71 unit tests covering config loading, database CRUD, API rotation, prompt rendering, and pipeline state — no external services required

---

## Results

- Processes a typical search run (200–500 jobs across multiple keywords and locations) end-to-end in under two hours
- Gemini screening backend achieves ~3× throughput over the local GGUF backend by running 3 concurrent API workers with rotating keys
- Reduces manual job review time from hours to minutes by surfacing only high-match roles with cover letters already drafted

---

*Built May 2026 · Source available on request*
