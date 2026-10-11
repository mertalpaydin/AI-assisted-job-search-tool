"""
Open-vocabulary skill extractor.
Extracts candidate skills directly from job ad descriptions and LinkedIn native tags
without AI or static YAML dictionaries.
"""
from __future__ import annotations

import json
import re
from typing import List, Optional, Set

from job_search.skills.filter_pipeline import SkillFilterPipeline, clean_candidate_token
from job_search.skills.normalizer import SkillNormalizer


# Headers indicating requirement / qualification sections
REQUIREMENT_HEADER_REGEX = re.compile(
    r"(?:(?:what\s+we(?:'re|\s+are)?\s+looking\s+for|who\s+you\s+are|requirements|qualifications|"
    r"what\s+you\s+(?:should\s+)?bring|your\s+profile|what\s+we\s+expect|skills\s*(?:&|\+)?\s*experience|"
    r"must[- ]haves?|anforderungen|ihr\s+profil|das\s+bringst\s+du\s+mit|tech\s+stack)[\s:]*\n)(.*?)"
    r"(?=(?:\n\s*(?:benefits|we\s+offer|what\s+we\s+offer|about\s+us|about\s+the\s+company|wir\s+bieten|"
    r"kontakt|apply\s+now|equal\s+opportunity|$)))",
    re.I | re.DOTALL,
)

# Grammatical trigger patterns for skill extraction
TRIGGER_PATTERNS = [
    re.compile(
        r"(?:experience\s+(?:in|with|building|using)|knowledge\s+of|proficiency\s+(?:in|with)|proficient\s+(?:in|with)|"
        r"skilled\s+in|familiarity\s+with|expertise\s+in|working\s+knowledge\s+of|hands-on\s+(?:experience\s+with|experience\s+in|with)|"
        r"strong\s+background\s+in|understanding\s+of|ability\s+to\s+use|technologies\s+such\s+as|tools\s+like|stack:\s*|skills:\s*|"
        r"familiar\s+with|background\s+with|erfahrung\s+(?:in|mit)|kenntnisse\s+(?:in|mit))\s+([^.\n]+)",
        re.I,
    ),
    # Parenthetical tech enumerations (e.g. "cloud infrastructure (AWS, GCP or Azure)", "deep learning (PyTorch, TensorFlow)")
    re.compile(
        r"\b(?:languages?|tools?|technologies|frameworks?|platforms?|infrastructure|databases?|experimentation)\s*\(([^)]+)\)",
        re.I,
    ),
]


class OpenSkillExtractor:
    """Extracts, filters, and normalizes skills directly from raw job ad texts."""

    def __init__(
        self,
        filter_pipeline: Optional[SkillFilterPipeline] = None,
        normalizer: Optional[SkillNormalizer] = None,
    ):
        self.filter = filter_pipeline or SkillFilterPipeline()
        self.normalizer = normalizer or SkillNormalizer()

    def extract_from_description(self, description: str) -> Set[str]:
        """
        Extracts verified canonical skills from job description text.
        Steps:
        1. Segment requirements section (fallback to full text if unsegmented).
        2. Apply grammatical trigger and parenthetical regex patterns.
        3. Split compound lists (comma, 'and', 'or', slashes, 'und', 'sowie').
        4. Clean tokens (strip fluff modifiers and conditions).
        5. Filter out syntactic garbage.
        6. Normalize and canonicalize spelling.
        """
        if not description:
            return set()

        # Step 1: Section Isolation
        sections = REQUIREMENT_HEADER_REGEX.findall(description)
        target_texts = sections if sections else [description]

        extracted_skills: Set[str] = set()

        for sec_text in target_texts:
            for pattern in TRIGGER_PATTERNS:
                for match in pattern.finditer(sec_text):
                    captured = match.group(1)
                    # Step 3: Split compound lists
                    parts = re.split(
                        r",\s*(?:and|or|und|sowie|oder)?\s*|\s+(?:and|or|und|sowie|oder|as well as)\s+|;\s*|/\s*(?![a-zA-Z0-9])",
                        captured,
                        flags=re.I,
                    )
                    for part in parts:
                        cleaned = clean_candidate_token(part)
                        if not cleaned:
                            continue
                        # Step 5: Filter out garbage
                        is_bad, _ = self.filter.is_garbage(cleaned)
                        if not is_bad:
                            # Step 6: Normalize canonical spelling
                            canonical = self.normalizer.normalize(cleaned)
                            if canonical:
                                extracted_skills.add(canonical)

        return extracted_skills

    def extract_from_linkedin_json(self, skills_json_str: str) -> Set[str]:
        """
        Extracts native LinkedIn tagged skills stored in the skillsDescription column.
        JSON format: {"text": "Python\\nSQL\\nPower BI\\nA/B Testing"}
        """
        if not skills_json_str:
            return set()

        skills: Set[str] = set()
        try:
            data = json.loads(skills_json_str)
            text_val = data.get("text", "")
            if not text_val:
                return set()
            lines = re.split(r"[\n,;]+", text_val)
            for line in lines:
                cleaned = clean_candidate_token(line)
                if not cleaned:
                    continue
                is_bad, _ = self.filter.is_garbage(cleaned)
                if not is_bad:
                    canonical = self.normalizer.normalize(cleaned)
                    if canonical:
                        skills.add(canonical)
        except (json.JSONDecodeError, AttributeError):
            pass

        return skills

    def extract_all(self, description: str, skills_description: Optional[str] = None) -> Set[str]:
        """Extract skills from both the description and the native LinkedIn tags."""
        skills = self.extract_from_description(description)
        if skills_description:
            skills.update(self.extract_from_linkedin_json(skills_description))
        return skills
