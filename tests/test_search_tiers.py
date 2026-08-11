"""Tests for tiered keyword scheduling in the search worker."""
from __future__ import annotations

from job_search.core.config import KeywordConfig, LocationConfig
from job_search.scraping.search import SearchWorker


def _worker(keywords: list[KeywordConfig], locations: list[LocationConfig]) -> SearchWorker:
    """Build a SearchWorker without touching the network or the database.

    _pairs_for_cycle only reads the keyword/location lists, so bypassing
    __init__ keeps this test free of Selenium/HTTP/DB fixtures.
    """
    worker = object.__new__(SearchWorker)
    worker._keywords = keywords
    worker._locations = locations
    worker._default_max_pages = 5
    worker._cycle_index = 0
    return worker


LOC = LocationConfig(geo_id="1", name="Test Location")


class TestPairsForCycle:
    def test_tier_one_runs_every_cycle(self) -> None:
        w = _worker([KeywordConfig(term="AI Transformation", tier=1)], [LOC])
        for index in range(6):
            assert len(w._pairs_for_cycle(index)) == 1

    def test_higher_tiers_run_less_often(self) -> None:
        w = _worker(
            [
                KeywordConfig(term="tier1", tier=1),
                KeywordConfig(term="tier2", tier=2),
                KeywordConfig(term="tier3", tier=3),
            ],
            [LOC],
        )
        due = lambda i: sorted(k.term for k, _ in w._pairs_for_cycle(i))
        assert due(0) == ["tier1", "tier2", "tier3"]   # all align on cycle 0
        assert due(1) == ["tier1"]
        assert due(2) == ["tier1", "tier2"]
        assert due(3) == ["tier1", "tier3"]
        assert due(4) == ["tier1", "tier2"]

    def test_pairs_cover_every_location(self) -> None:
        other = LocationConfig(geo_id="2", name="Other")
        w = _worker([KeywordConfig(term="AI", tier=1)], [LOC, other])
        pairs = w._pairs_for_cycle(0)
        assert [loc.geo_id for _, loc in pairs] == ["1", "2"]

    def test_empty_cycle_yields_no_pairs(self) -> None:
        w = _worker([KeywordConfig(term="rare", tier=4)], [LOC])
        assert w._pairs_for_cycle(1) == []
        assert w._pairs_for_cycle(3) == []
        assert len(w._pairs_for_cycle(4)) == 1
