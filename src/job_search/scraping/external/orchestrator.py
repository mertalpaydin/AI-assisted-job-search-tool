"""
External Search Orchestrator.
Coordinates searches across Indeed, Arbeitsagentur, SerpApi, and RapidAPI,
applying the exact same config, title prefilters, and blocked companies as LinkedIn,
cross-referencing with jobs.db, and saving to data/external_jobs.db without autoscreening.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from job_search.core.config import Config, load_config
from job_search.core.external_database import ExternalDatabaseManager
from job_search.core.prefilter import TitlePrefilter
from job_search.scraping.external.matcher import LinkedInMatcher
from job_search.scraping.external.providers import (
    ArbeitsagenturProvider,
    BaseProvider,
    IndeedProvider,
    RapidApiProvider,
    SerpApiProvider,
)


class ExternalSearchOrchestrator:
    """Manages multi-source external job extraction."""

    def __init__(
        self,
        config: Config | None = None,
        config_path: str | Path = "config/config.yaml",
        external_db: ExternalDatabaseManager | None = None,
    ) -> None:
        self.config = config or load_config(config_path)
        self.ext_db = external_db or ExternalDatabaseManager()
        self.title_prefilter = TitlePrefilter(self.config)
        self.blocked_companies = frozenset(
            c.lower().strip() for c in self.config.search.blocked_companies if c and c.strip()
        )
        self.matcher = LinkedInMatcher(self.config.database.path)

        # Initialize providers
        self.providers: dict[str, BaseProvider] = {
            "indeed": IndeedProvider(),
            "arbeitsagentur": ArbeitsagenturProvider(),
            "serpapi": SerpApiProvider(),
            "rapidapi": RapidApiProvider(),
        }

    def run_search(
        self,
        provider_names: list[str] | None = None,
        keywords_override: list[str] | None = None,
        location_override: str | None = None,
        limit_per_search: int = 10,
        progress_callback: Callable[[str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """
        Run searches across selected external providers.
        """
        # Determine providers
        selected_providers = []
        target_names = [p.lower().strip() for p in (provider_names or list(self.providers.keys()))]
        if "all" in target_names:
            target_names = list(self.providers.keys())

        for name in target_names:
            if name in self.providers:
                selected_providers.append(self.providers[name])

        # Determine keywords from config
        if keywords_override:
            keywords = keywords_override
        else:
            # Use top tier keywords by default
            keywords = [kw.term for kw in self.config.search.keywords]

        # Determine location from config
        if location_override:
            location = location_override
        else:
            location_entry = self.config.search.locations[0] if self.config.search.locations else None
            location = location_entry.name if location_entry else "Frankfurt, Germany"
            # Simplify location name for search queries
            if "Rhine-Main" in location:
                location = "Frankfurt, Germany"

        stats = {
            "total_found": 0,
            "new_inserted": 0,
            "updated": 0,
            "prefiltered_title": 0,
            "prefiltered_company": 0,
            "matched_linkedin": 0,
            "by_provider": {p.name: 0 for p in selected_providers},
        }

        def log_msg(msg: str) -> None:
            logger.info(msg)
            if progress_callback:
                progress_callback(msg)

        log_msg(f"Starting external search with {len(selected_providers)} providers for {len(keywords)} keywords in '{location}'")

        for kw in keywords:
            if should_stop and should_stop():
                log_msg("External search stopped by user request.")
                break

            for provider in selected_providers:
                if should_stop and should_stop():
                    log_msg("External search stopped by user request.")
                    break

                try:
                    log_msg(f"[{provider.name.upper()}] Searching for '{kw}'...")
                    raw_jobs = provider.search(keyword=kw, location=location, limit=limit_per_search)
                    stats["by_provider"][provider.name] += len(raw_jobs)
                    stats["total_found"] += len(raw_jobs)

                    for job_dict in raw_jobs:
                        if should_stop and should_stop():
                            log_msg("External search stopped by user request during processing.")
                            break
                        title = job_dict.get("title", "")
                        comp = job_dict.get("company_name", "")

                        # 1. Blocked company filter
                        if comp and comp.lower().strip() in self.blocked_companies:
                            job_dict["prefilter_status"] = "filtered_company"
                            job_dict["prefilter_reason"] = f"company:blocked ({comp})"
                            stats["prefiltered_company"] += 1
                        else:
                            # 2. Title prefilter (same as LinkedIn)
                            title_reason = self.title_prefilter.reason(title)
                            if title_reason:
                                job_dict["prefilter_status"] = "filtered_title"
                                job_dict["prefilter_reason"] = title_reason
                                stats["prefiltered_title"] += 1
                            else:
                                job_dict["prefilter_status"] = "accepted"

                        # 3. Match against LinkedIn jobs in jobs.db
                        if job_dict.get("prefilter_status") == "accepted":
                            match = self.matcher.find_match(comp, title)
                            if match:
                                job_dict.update(match)
                                stats["matched_linkedin"] += 1

                        # 4. Save to external_jobs.db
                        job_id, is_new = self.ext_db.upsert_job(job_dict)
                        if is_new:
                            stats["new_inserted"] += 1
                        else:
                            stats["updated"] += 1

                except Exception as pe:
                    logger.error("Error running provider {} on '{}': {}", provider.name, kw, pe)

        log_msg(
            f"External search finished. Found: {stats['total_found']} total, "
            f"New: {stats['new_inserted']}, Updated: {stats['updated']}, "
            f"LinkedIn Matched: {stats['matched_linkedin']}, "
            f"Prefiltered (Title): {stats['prefiltered_title']}, "
            f"Prefiltered (Company): {stats['prefiltered_company']}"
        )
        return stats
