# AI-Assisted Job Search Tool

An AI-enhanced LinkedIn job search assistant that automates job discovery, screening, and cover letter generation.

## Features

- **Automated job scraping** from LinkedIn (search + details extraction)
- **AI screening** using a local Unsloth/Mistral model to match jobs against your CV
- **Cover letter generation** via Gemini API with multi-key rotation
- **Concurrent pipeline** with graceful shutdown and resume capability
- **Database-driven state** for full resumability

## Project Status

| Phase | Status |
|-------|--------|
| Phase 0: LinkedIn Data Discovery | Complete |
| Phase 0.5: Cleanup & Organization | Complete |
| Phase 1: Project Setup | Pending |
| Phase 2: Database & Core | Pending |
| Phase 3: Scraping Refactor | Pending |
| Phase 4: AI Screening | Pending |
| Phase 5: Cover Letter Generation | Pending |
| Phase 6: Orchestration | Pending |
| Phase 7: Testing & Polish | Pending |

## Phase 0 Discoveries

LinkedIn API analysis of 3 sample job postings revealed:

- **136 unique fields** available across job and company data
- **87 fields selected** for the database schema (media files excluded)
- **6 tables** in the final schema: `jobs`, `companies`, `screening_results`, `cover_letters`, `processing_state`, `api_usage`

Key findings:
- `workRemoteAllowed` field available for remote filtering
- Rich company data nested in the `included` array of API responses
- Salary fields present but inconsistently populated
- `applies` and `views` engagement metrics available

Deliverables saved in `data/samples/`:
- `field_catalog.yaml` — all 136 fields categorized and rated
- `final_schema.sql` — finalized SQL schema
- `field_mappings.json` — JSON path extraction mappings
- `discovery_report.md` / `schema_summary.md` — summary reports
- `job_*.json` — 3 raw sample job API responses

## Setup

```bash
# Install dependencies with UV
uv sync

# Configure credentials
cp config/.env.example config/.env
# Edit config/.env with your LinkedIn credentials and Gemini API keys

# Configure job search settings
# Edit config/config.yaml with your keywords, locations, and preferences

# Run
uv run job-search
```

## Project Structure

```
├── config/             # Configuration files (config.yaml, cv.yaml, prompts.yaml, .env)
├── data/
│   └── samples/        # Phase 0 discovery outputs and sample JSON responses
├── scripts/
│   └── phase0_discovery/  # Phase 0 data discovery scripts (reference only)
├── src/
│   └── job_search/     # Main application package
│       ├── core/        # Config, database, state management
│       ├── scraping/    # LinkedIn search + details extraction
│       ├── ai/          # Screening + cover letter generation
│       ├── orchestration/  # Pipeline coordinator + workers
│       └── utils/       # Logging, retry, API rotation
├── codebase/           # Original scripts (reference/migration source)
└── tests/              # Test suite
```
