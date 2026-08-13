"""Deterministic pre-screening filters.

These run before the AI screener and cost nothing. A job that matches is
recorded with a `prefilter_reason` instead of being deleted, so every decision
stays auditable and a bad rule can be reversed by clearing the column.

Two stages, because the data arrives in two parts:

* :func:`TitlePrefilter.reason` runs on the search stub, where only the title
  is known. A match skips the LinkedIn detail request *and* the screening call.
* :func:`DetailsPrefilter.reason` runs after the detail fetch, where employment
  type, experience level and the description are available. A match skips the
  screening call only.
"""
from __future__ import annotations

import re

from job_search.core.config import Config


def _word_pattern(term: str) -> re.Pattern[str]:
    """Compile a term so it matches only as a whole word.

    A boundary is asserted on each side of the term, but only where the term's
    own edge is alphanumeric. That stops a term matching inside a longer word
    from either direction — ``ios`` must not fire on "Studios" (suffix) and
    ``intern`` must not fire on "International" (prefix) — while a term that
    ends in punctuation such as ``c++`` still matches "C++ Engineer".
    """
    t = term.lower()
    left = r"(?<![a-z0-9])" if t[:1].isalnum() else ""
    # A glued German gender ending ("…In", "…innen") is tolerated before the
    # boundary, so "werkstudent" still catches "WerkstudentIn" and "berater"
    # catches "BeraterIn", while "intern" still does not catch "International".
    right = r"(?:innen|in)?(?![a-z0-9])" if t[-1:].isalnum() else ""
    return re.compile(left + re.escape(t) + right, re.IGNORECASE)


def _compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(t, _word_pattern(t)) for t in terms if t and t.strip()]


class TitlePrefilter:
    """Title-only rules, evaluated against a search stub."""

    def __init__(self, config: Config) -> None:
        cfg = config.search.title_filter
        self._require = _compile_terms(cfg.require_any)
        self._exclude = _compile_terms(cfg.exclude_any)
        self._exclude_unless_ai = _compile_terms(cfg.exclude_unless_ai)
        self._ai_signal = _compile_terms(cfg.ai_signal)

    def has_ai_signal(self, title: str) -> bool:
        """True when the title mentions AI, ML or data work."""
        return any(rx.search(title) for _, rx in self._ai_signal)

    def reason(self, title: str | None) -> str | None:
        """Return a prefilter reason, or None when the title should proceed.

        A stub with no title always proceeds: it carries no evidence either
        way, and the detail fetch will resolve it.
        """
        if not title:
            return None

        for term, rx in self._exclude:
            if rx.search(title):
                return f"title:{term}"

        # Only rejected when nothing in the title suggests AI or data work, so
        # "Full-Stack AI Engineer" survives while "Full Stack Engineer" does not.
        if self._exclude_unless_ai and not self.has_ai_signal(title):
            for term, rx in self._exclude_unless_ai:
                if rx.search(title):
                    return f"title:{term} (no AI signal)"

        if self._require and not any(rx.search(title) for _, rx in self._require):
            return "title:no required keyword"

        return None


# A fluency marker only counts when it sits beside a German-language word, so a
# posting demanding "verhandlungssichere Englischkenntnisse" is not mistaken for
# one demanding German. The gap may not cross a sentence end or step over the
# word "English", which would otherwise attach the marker to the wrong language.
_LANG = r"(?:deutsch\w*|german)"
_MARK = (
    r"(?:verhandlungssicher\w*|verhandlungsf(?:ä|ae)hig\w*|flie(?:ß|ss)end\w*|fluent|"
    r"sehr\s+gute\w*|exzellente\w*|excellent|perfekte\w*|muttersprach\w*|native|"
    r"business[- ]level|c1|c2)"
)
_GAP = r"(?:(?!englisch|english)[^.;:\n]){0,25}"

GERMAN_FLUENCY_RE = re.compile(
    rf"{_MARK}{_GAP}{_LANG}|{_LANG}{_GAP}{_MARK}", re.IGNORECASE
)

# When German is explicitly optional the posting is still worth screening.
GERMAN_OPTIONAL_RE = re.compile(
    r"(is a plus|are a plus|nice to have|desirable|advantageous|ideally|preferred|"
    r"of advantage|a bonus|basic knowledge|von vorteil|w(?:ü|ue)nschenswert|"
    r"idealerweise|grundkenntnisse)",
    re.IGNORECASE,
)

_OPTIONAL_WINDOW = 60


def requires_fluent_german(description: str | None) -> bool:
    """True when the description demands fluent German as a hard requirement."""
    if not description:
        return False
    for match in GERMAN_FLUENCY_RE.finditer(description):
        start = max(0, match.start() - _OPTIONAL_WINDOW)
        end = min(len(description), match.end() + _OPTIONAL_WINDOW)
        if not GERMAN_OPTIONAL_RE.search(description[start:end]):
            return True
    return False


class DetailsPrefilter:
    """Rules that need the full job record."""

    def __init__(self, config: Config) -> None:
        cfg = config.search.prefilter
        self._enabled = cfg.enabled
        self._allowed_employment = {s.lower() for s in cfg.allowed_employment_status}
        self._excluded_levels = {s.lower() for s in cfg.excluded_experience_levels}
        self._reject_german = cfg.reject_fluent_german

    def reason(
        self,
        employment_status: str | None = None,
        experience_level: str | None = None,
        description: str | None = None,
    ) -> str | None:
        if not self._enabled:
            return None

        status = (employment_status or "").strip().lower()
        if self._allowed_employment and status and status not in self._allowed_employment:
            return f"employment:{employment_status}"

        level = (experience_level or "").strip().lower()
        if level and level in self._excluded_levels:
            return f"experience:{experience_level}"

        if self._reject_german and requires_fluent_german(description):
            return "german:fluent required"

        return None
