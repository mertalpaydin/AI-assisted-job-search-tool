from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class LocationConfig(BaseModel):
    geo_id: str
    name: str
    work_type: str | None = None  # "remote" | "onsite" | "hybrid" | None = any


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 30
    delay_between_requests: float = 2.0
    max_retries: int = 3
    idle_cycle_delay: float = 60.0  # seconds to wait after a full cycle with 0 new jobs


class KeywordConfig(BaseModel):
    """A single search term with its polling cadence and pagination depth.

    tier controls how often the term is searched: a term is included in cycle N
    only when N % tier == 0. tier 1 = every cycle, tier 2 = every second cycle,
    and so on. max_pages overrides SearchConfig.max_pages for this term only.
    """

    term: str
    tier: int = Field(default=1, ge=1)
    max_pages: int | None = None


# Title tokens marking a role as AI/data flavoured. Used by exclude_unless_ai.
_DEFAULT_AI_SIGNAL = [
    "ai", "ml", "genai", "llm", "nlp", "agentic", "artificial", "machine learning",
    "data", "analytics", "scientist", "mlops",
]


class TitleFilterConfig(BaseModel):
    require_any: list[str] = []  # at least one must match (word-boundary, case-insensitive); empty = disabled
    exclude_any: list[str] = []  # any match rejects the title outright
    exclude_unless_ai: list[str] = []  # rejects only when the title has no AI/data signal
    ai_signal: list[str] = Field(default_factory=lambda: list(_DEFAULT_AI_SIGNAL))


class PrefilterConfig(BaseModel):
    """Deterministic checks applied after the detail fetch, before screening."""

    enabled: bool = True
    allowed_employment_status: list[str] = ["Full-time"]
    excluded_experience_levels: list[str] = ["Internship"]
    reject_fluent_german: bool = True


class SearchConfig(BaseModel):
    keywords: list[KeywordConfig]
    locations: list[LocationConfig]
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    max_pages: int = 5  # pages of 100 results each, per keyword+location
    title_filter: TitleFilterConfig = Field(default_factory=TitleFilterConfig)
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)
    blocked_companies: list[str] = []  # company names to skip entirely (case-insensitive)

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, value: Any) -> Any:
        """Accept plain strings as well as mappings, so older configs keep working."""
        if not isinstance(value, list):
            return value
        return [{"term": v} if isinstance(v, str) else v for v in value]


class ScreeningModelConfig(BaseModel):
    path: str = "data/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf"
    n_gpu_layers: int = -1      # -1 = all layers on GPU
    n_ctx: int = 4096
    max_new_tokens: int = 512
    temperature: float = 0.1


class ScreeningCriteriaConfig(BaseModel):
    min_cv_match_score: float = 0.65
    max_german_level: str = "low"


class GeminiScreeningConfig(BaseModel):
    model: str = "gemini-3.1-flash-lite-preview"  # verify exact ID at ai.google.dev/gemini-api/docs/models
    temperature: float = 0.1
    max_tokens: int = 512
    requests_per_minute: int = 15         # per API key


class ScreeningConfig(BaseModel):
    backend: str = "local"                # "local" | "gemini"
    # auto routes on who is waiting: a scheduled run batches (nobody is
    # watching, so the 24h latency is free and the 50% saving is pure gain),
    # a manual run screens instantly.
    mode: str = "auto"                    # instant | batch | auto
    # Only applies to MANUAL runs under auto: a backlog too large to sit
    # through goes to batch even though you started it by hand.
    batch_threshold: int = 250
    batch_stale_after_hours: float = 36.0 # warn about a batch open longer than this
    # How often a running pipeline checks open batches and decides whether
    # enough new work has piled up to submit another one. Cheap: with no open
    # batches and nothing pending it is two indexed COUNT queries.
    batch_poll_minutes: float = 10.0
    model: ScreeningModelConfig = Field(default_factory=ScreeningModelConfig)
    gemini: GeminiScreeningConfig = Field(default_factory=GeminiScreeningConfig)
    criteria: ScreeningCriteriaConfig = Field(default_factory=ScreeningCriteriaConfig)


class CoverLetterRateLimitConfig(BaseModel):
    requests_per_minute: int = 15
    retry_delay: int = 60
    max_retries: int = 5


class CoverLetterConfig(BaseModel):
    mode: str = "auto"   # "auto" | "user_approval"
    model: str = "gemini-1.5-flash"
    temperature: float = 0.7
    max_tokens: int = 5000
    use_search_grounding: bool = True  # enable Google Search so Gemini can research the company
    rate_limits: CoverLetterRateLimitConfig = Field(default_factory=CoverLetterRateLimitConfig)


class ConcurrencyConfig(BaseModel):
    max_search_workers: int = 2
    max_details_workers: int = 3
    max_screening_workers: int = 3
    max_cover_letter_workers: int = 3


class AuthConfig(BaseModel):
    """LinkedIn session persistence and interactive login behaviour."""

    session_file: str = "data/linkedin_session.json"
    interactive_timeout: int = 300   # seconds to wait for a 2FA approval
    validate_on_start: bool = True


class ScheduleConfig(BaseModel):
    pause_file: str = "data/schedule.paused"
    default_pause: str = "tomorrow_morning"   # 12h | tomorrow_morning | 24h | indefinite
    morning_resume_hour: int = 7


class ShutdownConditionsConfig(BaseModel):
    no_new_jobs_minutes: int = 30
    check_interval_seconds: int = 60


class ExecutionConfig(BaseModel):
    max_runtime_hours: int = 8
    lock_file: str = "data/runner.lock"
    lock_stale_after_minutes: int = 120
    stop_file: str = "data/runner.stop"
    force_stop_grace_seconds: int = 30
    idle_drain_minutes: int = 2   # exit this soon after queues empty (non-search runs)
    shutdown_conditions: ShutdownConditionsConfig = Field(default_factory=ShutdownConditionsConfig)
    pickup_on_restart: bool = True
    checkpoint_interval_minutes: int = 5
    retry_errors_interval_minutes: int = 1  # 0 = disabled; retries errored jobs automatically


class DatabaseConfig(BaseModel):
    path: str = "data/jobs.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/job_search.log"


class WebUIConfig(BaseModel):
    auto_start: bool = True
    host: str = "127.0.0.1"
    port: int = 5000


class ExportConfig(BaseModel):
    output_dir: str = "data/export"
    pdf_dir: str = "data/export"


class Config(BaseModel):
    search: SearchConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    cover_letter: CoverLetterConfig = Field(default_factory=CoverLetterConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    web: WebUIConfig = Field(default_factory=WebUIConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)


# ---------------------------------------------------------------------------
# Secrets (from .env)
# ---------------------------------------------------------------------------

class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="config/.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    linkedin_username: str = ""
    linkedin_password: str = ""
    gemini_api_key_1: str = ""
    gemini_api_key_2: str = ""
    gemini_api_key_3: str = ""
    huggingface_token: str = ""

    @property
    def gemini_api_keys(self) -> list[str]:
        return [k for k in [self.gemini_api_key_1, self.gemini_api_key_2, self.gemini_api_key_3] if k]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _merge_filter_files(data: dict[str, Any], base_dir: Path) -> None:
    """Fold an external `search.filters_file` into the search config.

    The long title-filter keyword lists and the company block list are kept in
    their own document so config.yaml stays short. The referenced file supplies
    `title_filter` and/or `blocked_companies`; anything set inline in config.yaml
    still wins, so a one-off override does not require touching the shared file.
    """
    search = data.get("search")
    if not isinstance(search, dict):
        return
    ref = search.pop("filters_file", None)
    if not ref:
        return
    fpath = Path(ref)
    if not fpath.is_absolute():
        fpath = base_dir / fpath
    if not fpath.exists():
        raise FileNotFoundError(f"search.filters_file not found: {fpath}")
    with fpath.open(encoding="utf-8") as f:
        filters: dict[str, Any] = yaml.safe_load(f) or {}

    if "title_filter" in filters:
        merged = dict(filters.get("title_filter") or {})
        merged.update(search.get("title_filter") or {})  # inline overrides file
        search["title_filter"] = merged
    if "blocked_companies" in filters and "blocked_companies" not in search:
        search["blocked_companies"] = filters.get("blocked_companies") or []


def load_config(config_path: str = "config/config.yaml") -> Config:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    _merge_filter_files(data, path.parent)
    return Config.model_validate(data)


def load_secrets() -> Secrets:
    return Secrets()
