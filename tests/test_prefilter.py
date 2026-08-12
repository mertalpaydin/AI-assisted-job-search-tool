"""Tests for the deterministic prefilters that run before the AI screener."""
from __future__ import annotations

import pytest

from job_search.core.config import Config
from job_search.core.prefilter import (
    DetailsPrefilter,
    TitlePrefilter,
    requires_fluent_german,
)


def _config(**title_filter) -> Config:
    return Config.model_validate({
        "search": {
            "keywords": ["x"],
            "locations": [{"geo_id": "1", "name": "X"}],
            "title_filter": title_filter,
        },
    })


class TestTitlePrefilter:
    def test_missing_title_always_passes(self) -> None:
        """A stub with no title carries no evidence, so let details decide."""
        tp = TitlePrefilter(_config(exclude_any=["werkstudent"]))
        assert tp.reason(None) is None
        assert tp.reason("") is None

    def test_hard_exclusion_reports_the_matching_term(self) -> None:
        tp = TitlePrefilter(_config(exclude_any=["werkstudent"]))
        assert tp.reason("Werkstudent Data Science (m/w/d)") == "title:werkstudent"

    def test_exclusion_beats_a_required_keyword(self) -> None:
        tp = TitlePrefilter(_config(require_any=["data"], exclude_any=["werkstudent"]))
        assert tp.reason("Werkstudent Data Science") == "title:werkstudent"

    def test_missing_required_keyword_is_reported(self) -> None:
        tp = TitlePrefilter(_config(require_any=["ai", "data"]))
        assert tp.reason("Head of Physical Security") == "title:no required keyword"
        assert tp.reason("AI Transformation Lead") is None

    def test_empty_require_list_disables_the_requirement(self) -> None:
        tp = TitlePrefilter(_config(require_any=[]))
        assert tp.reason("Anything At All") is None

    @pytest.mark.parametrize("title", ["Studios Manager", "Scenarios Analyst"])
    def test_short_terms_do_not_match_inside_words(self, title: str) -> None:
        """'ios' must not fire on 'Studios'."""
        tp = TitlePrefilter(_config(exclude_any=["ios"]))
        assert tp.reason(title) is None

    def test_ios_still_matches_as_its_own_word(self) -> None:
        tp = TitlePrefilter(_config(exclude_any=["ios"]))
        assert tp.reason("Senior iOS Engineer") == "title:ios"

    def test_punctuation_terms_survive_escaping(self) -> None:
        """re.escape keeps 'c++' usable as a term."""
        tp = TitlePrefilter(_config(exclude_any=["c++"]))
        assert tp.reason("Senior C++ Engineer") == "title:c++"


class TestExcludeUnlessAI:
    @pytest.fixture()
    def tp(self) -> TitlePrefilter:
        return TitlePrefilter(_config(
            exclude_unless_ai=["full stack", "fullstack", "backend"],
            ai_signal=["ai", "ml", "data"],
        ))

    @pytest.mark.parametrize("title", [
        "Full Stack Engineer",
        "Senior Backend Engineer (Elixir)",
        "Fullstack Developer",
    ])
    def test_rejected_without_an_ai_signal(self, tp: TitlePrefilter, title: str) -> None:
        assert tp.reason(title) is not None

    @pytest.mark.parametrize("title", [
        "Full-Stack AI Engineer",
        "Senior Fullstack Applied AI Engineer",
        "Backend Engineer - AI Agents",
        "Full Stack Data Engineer",
    ])
    def test_kept_when_the_title_signals_ai_or_data(self, tp: TitlePrefilter, title: str) -> None:
        assert tp.reason(title) is None

    def test_reason_explains_why(self, tp: TitlePrefilter) -> None:
        assert "no AI signal" in tp.reason("Full Stack Engineer")


class TestGermanFluency:
    @pytest.mark.parametrize("text", [
        "verhandlungssicheres Deutsch",
        "fließende Deutschkenntnisse",
        "fliessend Deutsch",
        "sehr gute Deutschkenntnisse erforderlich",
        "Deutsch auf C1 Niveau",
        "Deutschkenntnisse verhandlungssicher",
        "Muttersprachliche Deutschkenntnisse",
        "German language skills at C2 level",
        "fluent German",
        "native German speaker",
    ])
    def test_hard_german_requirements_are_detected(self, text: str) -> None:
        assert requires_fluent_german(text)

    @pytest.mark.parametrize("text", [
        "Fluent English is required",
        "verhandlungssichere Englischkenntnisse",
        "Business fluent English and basic German",
        "sehr gute Englischkenntnisse",
        "English C1",
        "You speak English natively",
        "fluent in English, German is a plus",
    ])
    def test_english_requirements_are_not_mistaken_for_german(self, text: str) -> None:
        """The bug this guards: 'verhandlungssicher' alone matched Englisch."""
        assert not requires_fluent_german(text)

    @pytest.mark.parametrize("text", [
        "Fluency in German is a plus",
        "Deutschkenntnisse wünschenswert",
        "Ideally German speaking",
        "German of advantage",
    ])
    def test_optional_german_is_not_a_hard_requirement(self, text: str) -> None:
        assert not requires_fluent_german(text)

    def test_empty_description(self) -> None:
        assert not requires_fluent_german(None)
        assert not requires_fluent_german("")


class TestDetailsPrefilter:
    @pytest.fixture()
    def dp(self) -> DetailsPrefilter:
        return DetailsPrefilter(_config())

    def test_non_fulltime_is_rejected(self, dp: DetailsPrefilter) -> None:
        assert dp.reason(employment_status="Contract") == "employment:Contract"
        assert dp.reason(employment_status="Full-time") is None

    def test_unknown_employment_status_passes(self, dp: DetailsPrefilter) -> None:
        """LinkedIn leaves this blank often; absence is not evidence."""
        assert dp.reason(employment_status=None) is None
        assert dp.reason(employment_status="") is None

    def test_internship_experience_level_is_rejected(self, dp: DetailsPrefilter) -> None:
        assert dp.reason(experience_level="Internship") == "experience:Internship"
        assert dp.reason(experience_level="Mid-Senior level") is None

    def test_german_requirement_is_rejected(self, dp: DetailsPrefilter) -> None:
        assert dp.reason(description="Wir erwarten verhandlungssicheres Deutsch.") == "german:fluent required"

    def test_english_requirement_passes(self, dp: DetailsPrefilter) -> None:
        assert dp.reason(description="You need verhandlungssichere Englischkenntnisse.") is None

    def test_disabling_the_prefilter_passes_everything(self) -> None:
        cfg = Config.model_validate({
            "search": {
                "keywords": ["x"],
                "locations": [{"geo_id": "1", "name": "X"}],
                "prefilter": {"enabled": False},
            },
        })
        dp = DetailsPrefilter(cfg)
        assert dp.reason(employment_status="Contract", experience_level="Internship",
                         description="verhandlungssicheres Deutsch") is None
