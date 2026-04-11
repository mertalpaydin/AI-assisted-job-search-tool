# AI-Assisted Job Search Tool

Automates LinkedIn job discovery, AI screening against your CV, cover letter generation, and application tracking — all from your local machine.

## Features

- **Automated job scraping** — LinkedIn search + full details extraction with pagination (up to 500 results per keyword/location)
- **AI screening** — local GGUF model (via llama-cpp-python) scores each job against your CV and filters by German language requirement and location
- **Cover letter generation** — Gemini API with multi-key rotation and exponential backoff retries
- **Concurrent pipeline** — parallel search, details, screening, and cover letter workers with graceful shutdown and resume
- **Export** — self-contained text files (job info + cover letter) and a CSV index ready for applications
- **Application tracking** — mark jobs as applied / interviewing / offered / rejected
- **Web UI** — local Flask dashboard to review jobs, read cover letters, and track status

## Project Status

| Phase | Status |
|-------|--------|
| Phase 0: LinkedIn Data Discovery | Complete |
| Phase 0.5: Cleanup & Organization | Complete |
| Phase 1: Project Setup | Complete |
| Phase 2: Database & Core | Complete |
| Phase 3: Scraping Refactor | Complete |
| Phase 4: AI Screening | Complete |
| Phase 5: Cover Letter Generation | Complete |
| Phase 6: Orchestration | Complete |
| Phase 7: Testing & Polish | Complete |

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

> **GPU support for the screening model (required for reasonable speed):**
> ```bash
> uv pip install llama-cpp-python \
>   --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
>   --force-reinstall --no-cache-dir
> ```

### 2. Configure credentials

```bash
cp config/.env.example config/.env
```

Edit `config/.env`:

```env
LINKEDIN_USERNAME=your_email@example.com
LINKEDIN_PASSWORD=your_password
GEMINI_API_KEY_1=your_key_here
GEMINI_API_KEY_2=          # optional, for rotation
GEMINI_API_KEY_3=          # optional, for rotation
```

### 3. Configure your search

```bash
cp config/config.yaml.example config/config.yaml
cp config/cv.yaml.example     config/cv.yaml
```

- **`config/config.yaml`** — set your keywords, locations, and screening thresholds
- **`config/cv.yaml`** — fill in your real CV data (used by the AI screener and cover letter generator)

> Both files are gitignored and will never be committed.

### 4. Place your GGUF model

Download a GGUF model and place it in `data/models/`. The default path configured is:

```
data/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

Update `screening.model.path` in `config/config.yaml` if you use a different filename.

---

## Running

### Run the full pipeline

```bash
uv run job-search run
```

This opens a browser window to log in to LinkedIn (handles CAPTCHA/2FA manually), then starts the pipeline: search → scrape details → screen with local AI → generate cover letters.

### Resume after interruption

```bash
uv run job-search run --resume
```

Picks up from where it left off — pending jobs are restored from the database.

### All CLI commands

```bash
# Run pipeline
uv run job-search run [--config config/config.yaml] [--resume] [--log-level DEBUG]

# Export cover letters to data/export/
uv run job-search export
uv run job-search export --all          # include jobs without cover letters yet
uv run job-search export --output-dir path/to/folder

# Track application status
uv run job-search track <job_id> applied
uv run job-search track <job_id> interviewing
uv run job-search track <job_id> offered
uv run job-search track <job_id> rejected
uv run job-search track <job_id> clear

# List selected jobs in the terminal
uv run job-search list
uv run job-search list --status pending

# Start the web UI
uv run job-search web                   # http://127.0.0.1:5000/
uv run job-search web --port 8080
```

### Web UI

```bash
uv run job-search web
```

Open `http://127.0.0.1:5000/` in your browser:

| Page | What you see |
|------|-------------|
| Dashboard | Pipeline stats, application tracking counts |
| Selected Jobs | Table with match score, German req, cover letter status |
| Job Detail | Full cover letter with copy button, LinkedIn apply link, status update buttons |

### Export format

Each selected job with a generated cover letter gets its own file in `data/export/`:

```
Acme_Corp_Senior_Python_Developer_3827392.txt
```

The file contains the job header (title, company, URL, match score, screening notes) followed by the full cover letter — everything you need to apply in one place.

An `index.csv` is also written listing all selected jobs with their links and statuses.

---

## Running Tests

```bash
uv run pytest tests/ -v
```

71 unit tests covering config loading, database CRUD, API key rotation, prompt rendering, and pipeline state management. No external services required.

---

## Project Structure

```
├── config/
│   ├── config.yaml.example     # Search keywords, locations, screening thresholds
│   ├── cv.yaml.example         # CV template — copy to cv.yaml and fill in
│   ├── prompts.yaml            # AI prompt templates
│   └── .env.example            # Credentials template — copy to .env
├── data/
│   ├── models/                 # Place GGUF model files here (gitignored)
│   ├── export/                 # Cover letter export output (gitignored)
│   └── samples/                # Phase 0 discovery outputs and sample API responses
├── scripts/
│   └── phase0_discovery/       # LinkedIn API discovery scripts (reference)
├── src/
│   └── job_search/
│       ├── core/               # Config (Pydantic), database (SQLite), state management
│       ├── scraping/           # LinkedIn auth (Selenium), search + details workers
│       ├── ai/                 # GGUF screener, Gemini cover letter generator, prompt manager
│       ├── export/             # Cover letter export to files + CSV
│       ├── web/                # Flask dashboard (templates + routes)
│       ├── orchestration/      # Pipeline coordinator — wires all workers together
│       └── utils/              # Logging (loguru), Gemini API key rotation
└── tests/                      # pytest test suite
```

---

## Phase 0 Discoveries

LinkedIn API analysis of 3 sample job postings revealed:

- **136 unique fields** across job and company data
- **87 fields** selected for the database schema
- **6 tables**: `jobs`, `companies`, `screening_results`, `cover_letters`, `processing_state`, `api_usage`

Key findings: `workRemoteAllowed` for remote filtering, rich company data in the `included` array, salary fields present but inconsistently populated.

Deliverables in `data/samples/`: `field_catalog.yaml`, `final_schema.sql`, `field_mappings.json`, `discovery_report.md`, `schema_summary.md`, and 3 raw sample API responses.
