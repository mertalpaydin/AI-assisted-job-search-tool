"""Tests for pulling the screening object out of model output.

Structured output makes valid JSON the API's job, so this parser is the safety
net rather than the primary mechanism. It is still worth pinning: it is what
stands between a decorated or slightly malformed answer and a job being marked
as a screening error and re-billed on every retry.
"""
from __future__ import annotations

import json

import pytest

from job_search.ai.screener import _first_json_object, _parse_screening_json

_FIELDS = ('"reasoning": "why", "german_requirement_level": "high", '
           '"cv_match_score": 0.1, "archetype": "none"')


class TestOrdinaryOutput:
    def test_a_bare_object(self) -> None:
        assert _parse_screening_json("{" + _FIELDS + "}")["archetype"] == "none"

    def test_a_fenced_object(self) -> None:
        text = "```json\n{" + _FIELDS + "}\n```"
        assert _parse_screening_json(text)["cv_match_score"] == 0.1

    def test_prose_around_the_object(self) -> None:
        text = "Here is my assessment:\n{" + _FIELDS + "}\nHope that helps."
        assert _parse_screening_json(text)["german_requirement_level"] == "high"


class TestTheObservedFailure:
    """gemini-3.5-flash-lite opened a fence, wrote the fields, closed the brace.

    The opening brace never arrived, so the old parser found no JSON at all and
    the job was marked errored — then re-screened and re-billed on every
    auto-retry pass.
    """

    def test_a_missing_opening_brace_is_recovered(self) -> None:
        text = "```json\n  " + _FIELDS + "\n}\n```"
        result = _parse_screening_json(text)
        assert result["cv_match_score"] == 0.1
        assert result["archetype"] == "none"

    def test_missing_brace_without_a_fence(self) -> None:
        assert _parse_screening_json("  " + _FIELDS + "\n}")["reasoning"] == "why"


class TestNestedObjects:
    """The old non-greedy regex stopped at the first closing brace.

    That was the more dangerous bug: a nested object would have been truncated
    into a *different, valid-looking* object rather than raising, so wrong data
    reached the database silently.
    """

    def test_a_nested_object_survives_intact(self) -> None:
        payload = {
            "reasoning": "why",
            "german_requirement_level": "low",
            "cv_match_score": 0.8,
            "archetype": "A",
            "evidence": {"quote": "sehr gute Deutschkenntnisse", "line": 12},
        }
        parsed = _parse_screening_json(json.dumps(payload))
        assert parsed["archetype"] == "A"
        assert parsed["evidence"]["line"] == 12

    def test_a_brace_inside_a_string_does_not_end_the_object(self) -> None:
        text = '{"reasoning": "the template used {placeholder} syntax", "archetype": "B"}'
        assert _parse_screening_json(text)["archetype"] == "B"

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        text = '{"reasoning": "they said \\"nein\\" twice}", "archetype": "C"}'
        assert _parse_screening_json(text)["archetype"] == "C"


class TestFailures:
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_empty_output_says_so(self, text: str) -> None:
        with pytest.raises(ValueError, match="empty"):
            _parse_screening_json(text)

    def test_prose_with_no_object_still_raises(self) -> None:
        with pytest.raises(ValueError, match="No JSON found"):
            _parse_screening_json("I could not assess this role.")

    def test_the_error_does_not_dump_the_whole_response(self) -> None:
        """A full model answer in a WARNING line is what made this unreadable."""
        with pytest.raises(ValueError) as exc:
            _parse_screening_json("no json here " + "x" * 5000)
        assert len(str(exc.value)) < 600


class TestBraceScanner:
    def test_returns_none_without_an_opening_brace(self) -> None:
        assert _first_json_object("no braces at all") is None

    def test_returns_none_when_never_closed(self) -> None:
        assert _first_json_object('{"a": 1') is None

    def test_takes_the_first_complete_object_only(self) -> None:
        assert _first_json_object('{"a": 1} {"b": 2}') == '{"a": 1}'
