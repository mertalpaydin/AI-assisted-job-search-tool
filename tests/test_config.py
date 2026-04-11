"""Tests for job_search.core.config — loading and validation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from job_search.core.config import Config, Secrets, load_config


class TestLoadConfig:
    def test_loads_minimal_yaml(self, config_dir: Path) -> None:
        cfg = load_config(str(config_dir / "config.yaml"))
        assert isinstance(cfg, Config)
        assert cfg.search.keywords == ["Python Developer"]
        assert cfg.search.locations[0].geo_id == "102713980"
        assert cfg.search.locations[0].name == "Frankfurt am Main"

    def test_rate_limits_loaded(self, config_dir: Path) -> None:
        cfg = load_config(str(config_dir / "config.yaml"))
        assert cfg.search.rate_limits.requests_per_minute == 30
        assert cfg.search.rate_limits.delay_between_requests == 2.0
        assert cfg.search.rate_limits.max_retries == 3

    def test_defaults_applied(self, config_dir: Path) -> None:
        """Sub-models not present in YAML should use Pydantic defaults."""
        cfg = load_config(str(config_dir / "config.yaml"))
        assert cfg.screening.model.n_ctx == 4096
        assert cfg.cover_letter.model == "gemini-1.5-flash"
        assert cfg.concurrency.max_details_workers == 3
        assert cfg.execution.max_runtime_hours == 8
        assert cfg.database.path == "data/jobs.db"
        assert cfg.logging.level == "INFO"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        """Config with no 'search' key should fail Pydantic validation."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("screening:\n  model:\n    n_ctx: 1024\n", encoding="utf-8")
        with pytest.raises(Exception):  # pydantic.ValidationError
            load_config(str(bad))


class TestSecrets:
    def test_gemini_api_keys_filters_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY_1", "key-one")
        monkeypatch.setenv("GEMINI_API_KEY_2", "")
        monkeypatch.setenv("GEMINI_API_KEY_3", "key-three")
        # Create without reading .env file (env vars already set via monkeypatch)
        secrets = Secrets(_env_file=None)
        keys = secrets.gemini_api_keys
        assert "key-one" in keys
        assert "key-three" in keys
        assert "" not in keys

    def test_gemini_api_keys_all_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY_1", "")
        monkeypatch.setenv("GEMINI_API_KEY_2", "")
        monkeypatch.setenv("GEMINI_API_KEY_3", "")
        secrets = Secrets(_env_file=None)
        assert secrets.gemini_api_keys == []
