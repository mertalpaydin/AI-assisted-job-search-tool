"""
Deterministic garbage filtering pipeline for open-vocabulary skill extraction.
Discards syntactic noise, sentence fragments, active clauses, degree requirements,
and generic corporate filler without using AI.
"""
from __future__ import annotations

import re
from typing import Optional, Set, Tuple


# Standard English and German stopwords / generic filler tokens
STOP_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with", "by", "from",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "do", "does", "did",
    "we", "you", "they", "our", "your", "their", "must", "should", "would", "can", "could", "will",
    "strong", "good", "solid", "deep", "great", "excellent", "proven", "demonstrated",
    "relevant", "preferred", "required", "advantage", "plus", "ideal", "benefit",
    "minimum", "years", "year", "experience", "work", "job", "candidate", "role", "position",
    "degree", "bachelor", "master", "phd", "university", "school", "education",
    "skills", "ability", "knowledge", "understanding", "field", "area", "background",
    "team", "environment", "culture", "company", "clients", "customers", "stakeholders",
    "communication", "written", "verbal", "spoken", "fluent", "fluency", "english", "german",
    "problem", "solving", "analytical", "thinking", "mindset", "attitude",
    "one", "two", "three", "first", "second", "etc", "such", "as", "well", "both",
    "equivalent", "related", "similar", "appropriate", "various", "multiple",
    # German common stop tokens in job descriptions
    "und", "oder", "in", "mit", "von", "zu", "für", "auf", "ist", "sind", "haben", "wir", "sie",
    "deine", "ihre", "unser", "jahre", "berufserfahrung", "kenntnisse", "erfahrung", "erfolgreich",
    "abgeschlossenes", "studium", "deutsch", "englisch", "sehr", "gute", "fliessend",
    "wort", "schrift", "in wort und schrift", "wort und schrift", "deutschkenntnisse", "englischkenntnisse",
    "sprachkenntnisse", "fähigkeit", "fähigkeiten", "verhandlungssicher", "e.g", "eg", "i.e",
}

# Verbs that typically indicate an activity or duty rather than a skill noun phrase
ACTIVE_VERBS: Set[str] = {
    "is", "are", "have", "has", "had", "must", "should", "can", "will", "would",
    "working", "leading", "driving", "managing", "building", "supporting",
    "delivering", "overseeing", "handling", "maintaining", "presenting", "collaborating",
    "arbeiten", "führen", "entwickeln", "unterstützen",
}


def clean_candidate_token(token: str) -> str:
    """
    Strips leading bullets, numbers, fluff modifiers, and trailing qualifier text.
    E.g. '* Strong experience with Python (preferred)' -> 'Python'
    """
    if not token:
        return ""
    s = token.strip()
    # Strip leading bullets, numbers, hyphens
    s = re.sub(r"^[\*\-•·\d\.\)\s]+", "", s)
    # Strip leading fluff adjectives / qualifiers + prefixes like 'experience with'
    s = re.sub(
        r"^(?:such as|including|e\.?g\.?|strong|solid|proven|good|deep|extensive|practical|relevant|hands-on|deep understanding of)\s+"
        r"(?:experience\s+(?:with|in)\s+|knowledge\s+of\s+|background\s+in\s+)?",
        "", s, flags=re.I
    )
    # Strip trailing parenthetical condition phrases e.g. '(preferred)', '(a plus)'
    s = re.sub(
        r"\s*\([^)]*(?:preferred|plus|required|ideal|erwünscht|vorteil|advantage)[^)]*\)$",
        "", s, flags=re.I
    )
    # Strip trailing condition phrases
    s = re.sub(
        r"\s+(?:or similar|etc\.?|preferred|required|is a plus|as a plus|a plus|beneficial|strongly preferred)$",
        "", s, flags=re.I
    )
    # Strip trailing generic technical nouns e.g. 'CI/CD pipelines' -> 'CI/CD'
    s = re.sub(
        r"\s+(?:pipelines?|tools?|technologies|frameworks?|platforms?|libraries?)$",
        "", s, flags=re.I
    )
    # Strip trailing/leading punctuation
    return s.strip(" \t\n\r,.;:()[]{}'\"")


class SkillFilterPipeline:
    """Validates whether an extracted string is a valid skill candidate or noise."""

    def __init__(self, min_length: int = 2, max_length: int = 35, max_words: int = 4):
        self.min_length = min_length
        self.max_length = max_length
        self.max_words = max_words

    def is_garbage(self, candidate: str) -> Tuple[bool, str]:
        """
        Evaluate candidate. Returns (is_garbage, reason).
        If is_garbage is False, reason is 'valid'.
        """
        c_clean = candidate.strip()
        c_low = c_clean.lower()

        # Length check
        if len(c_clean) < self.min_length:
            return True, "too_short"
        if len(c_clean) > self.max_length:
            return True, "too_long"

        words = c_low.split()
        # Word count check
        if len(words) > self.max_words:
            return True, "too_many_words"

        # Stopword checks: single stopword prioritized over all_stopwords
        if len(words) == 1 and words[0] in STOP_WORDS:
            return True, "single_stopword"
        if all(w in STOP_WORDS for w in words):
            return True, "all_stopwords"

        # Experience duration check (e.g. '3+ years', '5 yrs', '3 Jahre')
        if re.search(r"\d+\+?\s*(?:year|month|yr|jahre)", c_low):
            return True, "years_experience"

        # Active verbal clause check
        if any(w in ACTIVE_VERBS for w in words):
            return True, "verbal_clause"

        # Degree / Academic boilerplate
        if re.search(r"\b(?:bachelor|master|degree|phd|diplom|abschluss|university)\b", c_low):
            return True, "degree_boilerplate"

        # Generic corporate / interpersonal filler
        if re.search(r"\b(?:team player|communication skills|fast-paced|stakeholders|culture|environment)\b", c_low):
            return True, "corporate_filler"

        # Invalid leading/trailing characters
        if re.match(r"^[^a-zA-Z0-9#+.]|[^a-zA-Z0-9#+.)]$", c_clean):
            return True, "punctuation_fluff"

        return False, "valid"
