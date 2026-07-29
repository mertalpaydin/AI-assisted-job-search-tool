# AI-Assisted Job Search Tool

Automates LinkedIn job discovery, AI screening against your CV, cover letter generation, 1-page LaTeX PDF exports, and application tracking — all from your local machine.

## Features

- **Automated job scraping** — LinkedIn search + full details extraction with pagination (up to 500 results per keyword/location)
- **AI screening** — Gemini API (default, multi-worker) or local GGUF model (via llama-cpp-python) scores each job against your CV and filters by German language requirement and location; backend is configurable per run
- **Cover letter generation** — Gemini API with multi-key rotation and exponential backoff retries
- **1-Page LaTeX Cover Letter PDF Exporter** — Automated single-page LaTeX cover letter PDF compiler using local MiKTeX (`xelatex` / `pdflatex`). Includes an executive centered header, custom tagline (`DATA SCIENCE & AI/ML SPECIALIST | STRATEGIC PROCUREMENT`), transparent signature image, dynamic "a/an" grammar selection, and an auto-fitting font-size algorithm to guarantee a 1-page fit
- **Customizable export locations** — Separate configurable output directories in `config/config.yaml` for CSV index/text exports (`export.output_dir`) and PDF cover letters (`export.pdf_dir`)
- **Concurrent pipeline** — parallel search, details, screening, and cover letter workers with graceful shutdown and resume
- **Application tracking** — mark jobs as applied / interviewing / offered / rejected
- **Web UI Dashboard** — local Flask dashboard to review jobs, edit title/company/cover letters live, generate PDFs with instant local file saving, copy cleaned MS Word text, and track application status

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

> **Note:** `llama-cpp-python` with GPU support is only needed when using the local GGUF screening backend (`screening.backend: "local"`). For the default Gemini backend, `uv sync` is sufficient.
>
> **GPU support for the local screening model (optional):**
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
GEMINI_API_KEY_1=your_key_here
GEMINI_API_KEY_2=          # optional, for rotation
GEMINI_API_KEY_3=          # optional, for rotation
```

### 3. Configure your search & export paths

```bash
cp config/config.yaml.example config/config.yaml
cp config/cv.yaml.example     config/cv.yaml
```

- **`config/config.yaml`** — set keywords, locations, screening thresholds, and export paths (`export.output_dir` and `export.pdf_dir`)
- **`config/cv.yaml`** — fill in your real CV data (used by the AI screener and cover letter generator)

> Both files are gitignored and will never be committed.

### 4. Place your GGUF model *(local backend only — skip if using Gemini)*

> **Skip this step** if `screening.backend` in `config/config.yaml` is set to `"gemini"` (the default).

Download a GGUF model and place it in `data/models/`. The default path configured is:

```
data/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

Update `screening.model.path` in `config/config.yaml` if you use a different filename.

### 5. Choose a screening backend

Set `screening.backend` in `config/config.yaml`:

| Value | Requires | Workers | Notes |
|-------|----------|---------|-------|
| `"gemini"` (default) | Gemini API keys in `.env` | Multiple (configurable) | Fast API calls; no GPU needed |
| `"local"` | GGUF model file + llama-cpp-python with CUDA | 1 (GPU-bound) | Fully offline |

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

### Run individual stages

Use `-s` / `--stages` to run only part of the pipeline. Pending jobs from previous runs are loaded from the database automatically.

```bash
# Screen pending jobs (no LinkedIn login needed)
uv run job-search run -s screen

# Generate cover letters for already-selected jobs
uv run job-search run -s cover-letter

# Screen and cover letter together
uv run job-search run -s screen -s cover-letter

# Scrape only, no AI
uv run job-search run -s search -s details
```

### Web UI & Live PDF Generator

```bash
uv run job-search web
```

Open `http://127.0.0.1:5000/` in your browser:

| Page | Features |
|------|----------|
| Dashboard | Pipeline stats, application tracking counts |
| Selected Jobs | Match score, German requirement, cover letter status |
| All Jobs | Every scraped job regardless of screening outcome |
| Search Stats | Per keyword + location stats and selection rates |
| Job Detail | Live editable Job Title, Company Name, Cover Letter Body, MS Word Copy, and instant **1-Page "Generate PDF"** button |

> **Live Unsaved Edits & PDF Generation:** Typing changes in the Job Title, Company Name, or Cover Letter body fields in the Web UI immediately feeds into the LaTeX PDF compiler upon clicking "Generate PDF", while auto-saving a live draft to the database.

### Export format

1. **LaTeX PDFs**: Generated on demand or via pipeline, saved directly to `export.pdf_dir` (e.g. `<your repo dir>\Desktop\CV\CL_saver\Applicant_Name_CoverLetter_Company.pdf`).
2. **Text Files**: Each selected job with a generated cover letter gets a `.txt` summary file in `export.output_dir`.
3. **Index CSV**: `index.csv` listing all selected jobs with their links and statuses.

---

## Running Tests

```bash
uv run pytest tests/ -v
```

87 unit tests covering config loading, database CRUD, API key rotation, prompt rendering, Web UI routes, and 1-page LaTeX PDF compilation. No external services required.

---

## Project Structure

```
├── config/
│   ├── config.yaml.example     # Search keywords, locations, export paths
│   ├── cv.yaml.example         # CV template — copy to cv.yaml and fill in
│   ├── prompts.yaml            # AI prompt templates
│   ├── cover_letter_template.tex # Executive LaTeX cover letter template
│   └── .env.example            # Credentials template — copy to .env
├── data/
│   ├── models/                 # Place GGUF model files here (gitignored)
│   ├── export/                 # Cover letter export output (gitignored)
│   └── samples/                # Phase 0 discovery outputs and sample API responses
├── docs/
│   └── project-summary.md      # CV-ready one-page project summary
├── scripts/
│   ├── view_db.py              # Standalone web UI — browse DB without running pipeline
│   ├── export_db.py            # Export all DB tables to CSV
│   └── fix_incomplete_cover_letters.py # Fix truncated cover letter entries
├── src/
│   └── job_search/
│       ├── core/               # Config (Pydantic), database (SQLite), state management
│       ├── scraping/           # LinkedIn auth (Selenium), search + details workers
│       ├── ai/                 # Gemini screener + local GGUF screener, cover letter generator
│       ├── export/             # Text/CSV exporter + LaTeX 1-page PDF exporter
│       ├── web/                # Flask dashboard (templates + routes)
│       ├── orchestration/      # Pipeline coordinator — wires all workers together
│       └── utils/              # Logging (loguru), Gemini API key rotation
└── tests/                      # pytest test suite (87 tests)
```
