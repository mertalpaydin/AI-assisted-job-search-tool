"""The screening leg must not silently screen a partial list.

Screening is fed by the details stage, not by a database poll, so a run that
screens WITHOUT scraping details sees only the jobs that already had them and
leaves the rest for another day. In the log that looked identical to "nothing
to do", which is how a details backlog grew unnoticed while every morning
piled more on top.
"""
from __future__ import annotations

import pytest

from job_search.core.config import Config
from job_search.core.database import DatabaseManager
from job_search.orchestration import coordinator as coord


@pytest.fixture()
def config(db: DatabaseManager, tmp_path) -> Config:
    return Config.model_validate({
        "search": {"keywords": ["AI"],
                   "locations": [{"geo_id": "1", "name": "F"}]},
        "database": {"path": str(db._path)},
        "execution": {"lock_file": str(tmp_path / "runner.lock"),
                      "stop_file": str(tmp_path / "runner.stop")},
    })


@pytest.fixture()
def warnings(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        coord.logger, "warning",
        lambda msg, *a, **k: seen.append(str(msg).format(*a) if a else str(msg)),
    )
    return seen


def _coordinator(config, db, stages):
    c = coord.JobSearchCoordinator(config, stages=stages)
    c._db.close()
    c._db = db
    return c


def _job_awaiting_details(db: DatabaseManager, job_id: int) -> None:
    db.insert_job(job_id, "kw", "loc")          # scraped = 0


def test_screening_without_details_warns_about_the_backlog(
    config, db, warnings
) -> None:
    for jid in range(1, 4):
        _job_awaiting_details(db, jid)

    _coordinator(config, db, {"screen"})._warn_if_screening_a_partial_list()

    assert len(warnings) == 1
    assert "PARTIAL" in warnings[0]
    assert "3 job(s)" in warnings[0]


def test_a_leg_that_scrapes_details_first_does_not_warn(config, db, warnings) -> None:
    """This is the fix: details in the same leg, so nothing is left behind."""
    for jid in range(1, 4):
        _job_awaiting_details(db, jid)

    _coordinator(
        config, db, {"details", "screen", "cover-letter"}
    )._warn_if_screening_a_partial_list()

    assert warnings == []


def test_no_backlog_means_no_warning(config, db, warnings) -> None:
    _coordinator(config, db, {"screen"})._warn_if_screening_a_partial_list()
    assert warnings == []


def test_prefiltered_jobs_are_not_counted_as_a_backlog(config, db, warnings) -> None:
    """A title-rejected job is work declined, not work waiting."""
    db.insert_job(1, "kw", "loc", prefilter_reason="title:excluded", title="Chef")

    _coordinator(config, db, {"screen"})._warn_if_screening_a_partial_list()

    assert warnings == []
