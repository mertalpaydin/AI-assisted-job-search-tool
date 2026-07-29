# AI-Assisted Job Search Tool

Automates LinkedIn job discovery, AI screening against your CV, cover letter generation, 1-page LaTeX PDF exports, job expiration cleaning, and application tracking — all from your local machine.

---

## Features

- **Automated Job Scraping** — LinkedIn search + full details extraction with pagination (up to 500 results per keyword/location) using Selenium with cookie session persistence.
- **Dual-Backend AI Screening** — Scores jobs against your CV and filters out jobs requiring high German proficiency or wrong locations. Supports:
  - **Gemini API** (default): Fast parallel screening with multi-key API rotation and exponential backoff retries.
  - **Local GGUF Model** (offline): GPU-accelerated local GGUF model via `llama-cpp-python`.
- **AI Cover Letter Generation** — Tailors cover letters to each selected job using your CV data and prompt guidelines.
- **1-Page LaTeX Cover Letter PDF Exporter** — Automated 1-page LaTeX cover letter compiler using local MiKTeX (`xelatex` / `pdflatex`). Features an executive centered header, tagline (`DATA SCIENCE & AI/ML SPECIALIST | STRATEGIC PROCUREMENT`), transparent signature image, dynamic "a/an" article selection, and an auto-fitting font size algorithm.
- **Customizable Export Locations** — Configurable output directories in `config/config.yaml` for CSV/Text exports (`export.output_dir`) and PDF cover letters (`export.pdf_dir`).
- **Job Expiration Cleaner** — Automated check to detect expired or closed LinkedIn job postings (`uv run job-search clean`).
- **Concurrent Pipeline** — Parallel search, details, screening, and cover letter workers with graceful shutdown, checkpointing, and resume capabilities.
- **Application Tracking** — Mark jobs as `applied`, `interviewing`, `offered`, `rejected`, or `clear`.
- **Web UI Dashboard** — Local Flask web application (`http://127.0.0.1:5000/`) to review jobs, edit Job Title / Company Name / Cover Letter text live, generate 1-page PDFs instantly, copy formatted text for MS Word, and track status.

---

## Project Status

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 0: Data Discovery | Complete | Scraped & cataloged 136 LinkedIn API fields into 6 SQLite tables |
| Phase 0.5: Cleanup & Organization | Complete | Established modular architecture & dependency management |
| Phase 1: Project Setup | Complete | Pydantic config schemas, logging, and environment settings |
| Phase 2: Database & Core | Complete | SQLite thread-safe DatabaseManager with WAL mode |
| Phase 3: Scraping Refactor | Complete | Selenium LinkedIn worker with cookie persistence |
| Phase 4: AI Screening | Complete | Gemini API screener (multi-key rotation) + Local GGUF screener |
| Phase 5: Cover Letter Generation | Complete | Gemini cover letter generator with prompt customization |
| Phase 6: Orchestration | Complete | Parallel pipeline coordinator with resume & stage scoping |
| Phase 7: Web UI & LaTeX PDF Export | Complete | Flask web dashboard, MS Word line formatting, 1-page LaTeX PDF generator |

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

> **Note:** `llama-cpp-python` with GPU support is only needed when using the local GGUF screening backend (`screening.backend: "local"`). For the default Gemini backend, standard `uv sync` is sufficient.
>
> **Optional CUDA GPU support for local GGUF screening:**
> ```bash
> uv pip install llama-cpp-python \
>   --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu128 \
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
GEMINI_API_KEY_1=your_primary_key_here
GEMINI_API_KEY_2=          # optional API key for rotation
GEMINI_API_KEY_3=          # optional API key for rotation
```

### 3. Configure search, CV, and export paths

```bash
cp config/config.yaml.example config/config.yaml
cp config/cv.yaml.example     config/cv.yaml
```

- **`config/config.yaml`** — set keywords, locations, screening thresholds, and export directories (`export.output_dir` and `export.pdf_dir`)
- **`config/cv.yaml`** — fill in your real CV data (used by the AI screener and cover letter generator)

> Both files are gitignored and will never be committed to repository history.

### 4. Choose a screening backend

Set `screening.backend` in `config/config.yaml`:

| Value | Requires | Workers | Notes |
|-------|----------|---------|-------|
| `"gemini"` (default) | Gemini API keys in `.env` | Multiple (configurable) | Fast API calls; no GPU needed |
| `"local"` | GGUF model file in `data/models/` + llama-cpp-python | 1 (GPU-bound) | Fully offline screening |

---

## Running the Tool

### Run the full pipeline

```bash
uv run job-search run
```

Opens a browser window to log in to LinkedIn (handles CAPTCHA/2FA manually), then executes the full pipeline: search → scrape details → screen jobs → generate cover letters.

### Resume after interruption

```bash
uv run job-search run --resume
```

Restores pending jobs from SQLite and resumes from the exact checkpoint.

### Run specific pipeline stages

Use `-s` / `--stages` to run only desired stages:

```bash
# Screen pending jobs only (no LinkedIn login needed)
uv run job-search run -s screen

# Generate cover letters for selected jobs
uv run job-search run -s cover-letter

# Screen and generate cover letters together
uv run job-search run -s screen -s cover-letter

# Scrape jobs only (no AI processing)
uv run job-search run -s search -s details
```

### Clean expired LinkedIn postings

```bash
uv run job-search clean
uv run job-search clean --limit 50
```

Checks pending jobs against LinkedIn to detect closed postings and marks them as `expired`.

### Reset pipeline errors for retry

```bash
# Reset all error types (details, screening, cover letter)
uv run job-search reset-errors

# Reset only detail scraping errors
uv run job-search reset-errors --stage details

# Reset only screening errors
uv run job-search reset-errors --stage screening
```

### Terminal Job Listing & Application Tracking

```bash
# List AI-selected jobs in terminal
uv run job-search list
uv run job-search list --status pending

# Update application status
uv run job-search track <job_id> applied
uv run job-search track <job_id> interviewing
uv run job-search track <job_id> offered
uv run job-search track <job_id> rejected
uv run job-search track <job_id> clear
```

### Export Cover Letters & CSV Index

```bash
uv run job-search export
uv run job-search export --output-dir data/export
```

---

## Web UI Dashboard

Start the Flask dashboard:

```bash
uv run job-search web
```

Open `http://127.0.0.1:5000/` in your browser.

| Dashboard Page | Capabilities |
|----------------|--------------|
| **Dashboard** | Overview metrics, application stage breakdowns, status counters |
| **Selected Jobs** | Table of AI-matched jobs, match scores, German requirement flags |
| **All Jobs** | Master repository of all scraped jobs |
| **Search Stats** | Conversion funnel metrics per keyword/location |
| **Job Detail** | Live editable **Job Title**, **Company Name**, and **Cover Letter Text**; MS Word clipboard formatter (`Copy Text`); instant **1-Page "Generate PDF"** compiler |

---

## Cover Letter PDF & Export Formats

### 1-Page LaTeX PDF Cover Letters
When clicking **"Generate PDF"** on the Web UI or calling the exporter:
- PDF is compiled using your local MiKTeX installation (`xelatex` / `pdflatex`).
- Saved directly to `export.pdf_dir` (e.g. `<your repo dir>\Desktop\CV\CL_saver\Applicant_Name_CoverLetter_[Company].pdf`).
- Features a centered executive header, tagline (`DATA SCIENCE & AI/ML SPECIALIST | STRATEGIC PROCUREMENT`), transparent signature image, dynamic "a/an" article selection (`an AI Engineer` vs `a Data Scientist`), and an auto-fitting font-size algorithm to guarantee a single-page output.

### Text & CSV Exports
- **Text Files**: Generated in `export.output_dir` (e.g. `Company_Title_JobId.txt`) containing metadata, match score, notes, and cover letter.
- **`index.csv`**: Master summary CSV listing all selected jobs, URLs, and application statuses.

---

## Running Tests

```bash
uv run pytest tests/ -v
```

87 automated unit tests covering:
- Database CRUD and WAL mode transaction safety
- Pydantic configuration schemas and `.env` credentials loading
- Gemini API key rotation and exponential backoff
- Prompt template manager and markdown formatting
- Job cleaner & expiration detection
- Flask Web UI routes, MS Word line break formatting, and live PDF endpoints
- 1-Page LaTeX PDF compiler and dynamic article logic

---

## Project Structure

```
├── config/
│   ├── config.yaml.example          # Search keywords, locations, export paths template
│   ├── cv.yaml.example              # CV template — copy to cv.yaml and fill in
│   ├── prompts.yaml                 # AI screening and cover letter prompt templates
│   ├── cover_letter_template.tex    # Executive 1-page LaTeX cover letter template
│   └── .env.example                 # Credentials template — copy to .env
├── data/
│   ├── models/                      # GGUF model files for local screening (gitignored)
│   ├── export/                      # Default text/CSV export directory (gitignored)
│   └── samples/                     # Discovery field catalog and SQL schemas
├── docs/
│   └── project-summary.md           # One-page project architectural summary
├── scripts/
│   ├── cleanup_errors.py            # Standalone script to clear pipeline errors
│   ├── export_db.py                 # Export database tables to CSV
│   ├── fix_incomplete_cover_letters.py # Purge incomplete/truncated cover letter entries
│   ├── test_login.py                # Test LinkedIn auth & session cookie saving
│   └── view_db.py                   # Standalone SQLite database viewer
├── src/
│   └── job_search/
│       ├── ai/                      # Gemini screener, local GGUF screener, cover letter generator
│       ├── cleaner/                 # LinkedIn job expiration cleaner
│       ├── core/                    # Pydantic config, SQLite database manager, pipeline state
│       ├── export/                  # Text/CSV exporter & 1-page LaTeX PDF exporter
│       ├── orchestration/           # Parallel pipeline coordinator & worker pools
│       ├── scraping/                # Selenium LinkedIn auth & details scraping workers
│       ├── utils/                   # Logging (loguru), Gemini API key rotation, formatting helpers
│       └── web/                     # Flask web dashboard (templates, static CSS, routes)
└── tests/                           # Unit test suite (87 tests)
```
