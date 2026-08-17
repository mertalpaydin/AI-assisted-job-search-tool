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
        assert [k.term for k in cfg.search.keywords] == ["Python Developer"]
        # bare strings coerce to tier 1 with no per-term page override
        assert cfg.search.keywords[0].tier == 1
        assert cfg.search.keywords[0].max_pages is None
        assert cfg.search.locations[0].geo_id == "102713980"
        assert cfg.search.locations[0].name == "Frankfurt am Main"

    def test_keywords_accept_mapping_form(self, tmp_path: Path) -> None:
        """Tiered mapping entries and bare strings can be mixed in one list."""
        path = tmp_path / "config.yaml"
        path.write_text(
            "search:\n"
            "  keywords:\n"
            '    - {term: "AI Transformation", tier: 1, max_pages: 10}\n'
            '    - {term: "Data Scientist", tier: 3, max_pages: 3}\n'
            '    - "Procurement"\n'
            "  locations:\n"
            '    - geo_id: "102713980"\n'
            '      name: "Frankfurt am Main"\n',
            encoding="utf-8",
        )
        cfg = load_config(str(path))
        assert [k.term for k in cfg.search.keywords] == [
            "AI Transformation", "Data Scientist", "Procurement",
        ]
        assert [k.tier for k in cfg.search.keywords] == [1, 3, 1]
        assert [k.max_pages for k in cfg.search.keywords] == [10, 3, None]

    def test_keyword_tier_must_be_positive(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(
            "search:\n"
            "  keywords:\n"
            '    - {term: "AI", tier: 0}\n'
            "  locations:\n"
            '    - geo_id: "1"\n'
            '      name: "X"\n',
            encoding="utf-8",
        )
        with pytest.raises(Exception):
            load_config(str(path))

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


class TestFiltersFile:
    def _write(self, tmp_path, config_body: str, filters_body: str):
        (tmp_path / "filters.yaml").write_text(filters_body, encoding="utf-8")
        p = tmp_path / "config.yaml"
        p.write_text(config_body, encoding="utf-8")
        return p

    def test_filters_file_supplies_lists(self, tmp_path) -> None:
        p = self._write(
            tmp_path,
            "search:\n  keywords: ['x']\n  locations: [{geo_id: '1', name: 'X'}]\n"
            "  filters_file: 'filters.yaml'\n",
            "title_filter:\n  require_any: ['ai', 'einkauf']\n  exclude_any: ['intern']\n"
            "blocked_companies: ['Acme Recruiting', 'Staffline']\n",
        )
        cfg = load_config(str(p))
        assert cfg.search.title_filter.require_any == ['ai', 'einkauf']
        assert cfg.search.title_filter.exclude_any == ['intern']
        assert cfg.search.blocked_companies == ['Acme Recruiting', 'Staffline']

    def test_inline_title_filter_overrides_file(self, tmp_path) -> None:
        p = self._write(
            tmp_path,
            "search:\n  keywords: ['x']\n  locations: [{geo_id: '1', name: 'X'}]\n"
            "  filters_file: 'filters.yaml'\n  title_filter:\n    require_any: ['data']\n",
            "title_filter:\n  require_any: ['ai']\n",
        )
        cfg = load_config(str(p))
        assert cfg.search.title_filter.require_any == ['data']

    def test_missing_filters_file_raises(self, tmp_path) -> None:
        p = tmp_path / "config.yaml"
        p.write_text(
            "search:\n  keywords: ['x']\n  locations: [{geo_id: '1', name: 'X'}]\n"
            "  filters_file: 'nope.yaml'\n", encoding="utf-8")
        with pytest.raises(FileNotFoundError):
            load_config(str(p))
