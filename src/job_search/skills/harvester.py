"""
Corpus-level skill harvester and cross-company dispersion filter.
Aggregates skills extracted across job postings, measures company dispersion,
and filters out idiosyncratic residual noise.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from job_search.skills.open_extractor import OpenSkillExtractor


@dataclass
class HarvestedSkill:
    """Represents a discovered skill with its statistical footprint across the corpus."""
    name: str
    frequency: int = 0
    company_count: int = 0
    companies: Set[str] = field(default_factory=set)
    job_ids: Set[int] = field(default_factory=set)
    is_linkedin_native: bool = False
    is_high_confidence: bool = False


class SkillHarvester:
    """
    Harvests skills across job descriptions and manages multi-company dispersion verification.
    """

    def __init__(self, extractor: Optional[OpenSkillExtractor] = None, min_companies: int = 2):
        self.extractor = extractor or OpenSkillExtractor()
        self.min_companies = min_companies
        self.skills: Dict[str, HarvestedSkill] = {}

    def process_job(
        self,
        job_id: int,
        company_name: Optional[str],
        description: str,
        skills_description: Optional[str] = None,
    ) -> Set[str]:
        """
        Process a single job ad, extracting its skills and updating corpus-level statistics.
        Returns the set of skills extracted for this job.
        """
        company = (company_name or "Unknown").strip()
        job_skills = self.extractor.extract_from_description(description)
        li_skills = self.extractor.extract_from_linkedin_json(skills_description or "")

        all_skills = job_skills.union(li_skills)

        for skill_name in all_skills:
            if skill_name not in self.skills:
                self.skills[skill_name] = HarvestedSkill(name=skill_name)
            record = self.skills[skill_name]
            record.frequency += 1
            record.companies.add(company)
            record.company_count = len(record.companies)
            record.job_ids.add(job_id)
            if skill_name in li_skills:
                record.is_linkedin_native = True
            if record.company_count >= self.min_companies or record.is_linkedin_native:
                record.is_high_confidence = True

        return all_skills

    def get_high_confidence_skills(self) -> List[HarvestedSkill]:
        """Returns all skills confirmed across >= min_companies or backed by native LinkedIn tags."""
        return [
            s for s in sorted(self.skills.values(), key=lambda x: x.frequency, reverse=True)
            if s.is_high_confidence
        ]

    def get_verified_skill_names(self) -> Set[str]:
        """Returns the set of verified high-confidence skill names."""
        return {s.name for s in self.get_high_confidence_skills()}
