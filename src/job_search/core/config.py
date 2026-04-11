from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class LocationConfig(BaseModel):
    geo_id: str
    name: str


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 30
    delay_between_requests: float = 2.0
    max_retries: int = 3


class SearchConfig(BaseModel):
    keywords: list[str]
    locations: list[LocationConfig]
    rate_limits: RateLimitConfig = Field(default_factory=RateLimitConfig)
    max_pages: int = 5  # pages of 100 results each, per keyword+location


class ScreeningModelConfig(BaseModel):
    path: str = "data/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf"
    n_gpu_layers: int = -1      # -1 = all layers on GPU
    n_ctx: int = 4096
    max_new_tokens: int = 512
    temperature: float = 0.1


class ScreeningCriteriaConfig(BaseModel):
    min_cv_match_score: float = 0.65
    max_german_level: str = "low"
    preferred_locations: list[str] = Field(default_factory=list)


class ScreeningConfig(BaseModel):
    model: ScreeningModelConfig = Field(default_factory=ScreeningModelConfig)
    criteria: ScreeningCriteriaConfig = Field(default_factory=ScreeningCriteriaConfig)


class CoverLetterRateLimitConfig(BaseModel):
    requests_per_minute: int = 15
    retry_delay: int = 60
    max_retries: int = 5


class CoverLetterConfig(BaseModel):
    model: str = "gemini-1.5-flash"
    temperature: float = 0.7
    max_tokens: int = 1000
    rate_limits: CoverLetterRateLimitConfig = Field(default_factory=CoverLetterRateLimitConfig)


class ConcurrencyConfig(BaseModel):
    max_search_workers: int = 2
    max_details_workers: int = 3
    max_screening_workers: int = 1
    max_cover_letter_workers: int = 3


class ShutdownConditionsConfig(BaseModel):
    no_new_jobs_minutes: int = 30
    check_interval_seconds: int = 60


class ExecutionConfig(BaseModel):
    max_runtime_hours: int = 8
    shutdown_conditions: ShutdownConditionsConfig = Field(default_factory=ShutdownConditionsConfig)
    pickup_on_restart: bool = True
    checkpoint_interval_minutes: int = 5


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


class Config(BaseModel):
    search: SearchConfig
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

def load_config(config_path: str = "config/config.yaml") -> Config:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return Config.model_validate(data)


def load_secrets() -> Secrets:
    return Secrets()
