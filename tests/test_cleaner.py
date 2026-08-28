from __future__ import annotations

from unittest.mock import MagicMock
import pytest
import requests

from job_search.cleaner.cleaner import JobCleaner
from job_search.core.database import DatabaseManager


def test_is_job_expired_closed_html(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    cleaner = JobCleaner(db)

    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption>'
    mock_session.get.return_value = mock_resp

    cleaner._session = mock_session
    assert cleaner.is_job_expired(4423912764) is True


def test_is_job_expired_301_redirect(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    cleaner = JobCleaner(db)

    mock_session = MagicMock(spec=requests.Session)
    mock_guest_resp = MagicMock()
    mock_guest_resp.status_code = 500  # API fallback

    mock_view_resp = MagicMock()
    mock_view_resp.status_code = 301
    mock_view_resp.headers = {"Location": "https://de.linkedin.com/jobs/analyst-stellen?trk=expired_jd_redirect"}

    mock_session.get.side_effect = [mock_guest_resp, mock_view_resp]
    cleaner._session = mock_session

    assert cleaner.is_job_expired(4423912764) is True


def test_is_job_expired_direct_view_text(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    cleaner = JobCleaner(db)

    mock_session = MagicMock(spec=requests.Session)
    mock_guest_resp = MagicMock()
    mock_guest_resp.status_code = 500  # Guest API fails

    mock_view_resp = MagicMock()
    mock_view_resp.status_code = 200
    mock_view_resp.text = '<figure class="closed-job closed-job__flavor topcard__flavor-row"><figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></figure>'

    mock_session.get.side_effect = [mock_guest_resp, mock_view_resp]
    cleaner._session = mock_session

    assert cleaner.is_job_expired(4439226671) is True


def test_clean_pending_jobs(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))

    # Insert a job
    db.insert_job(1234567, keyword="test", location_id="test")

    cleaner = JobCleaner(db)
    cleaner.is_job_expired = MagicMock(return_value=True)

    result = cleaner.clean_pending_jobs(limit=10)
    assert result["checked"] == 1
    assert result["expired"] == 1
    assert result["expired_ids"] == [1234567]

    # Verify status in DB
    job = db.get_selected_job(1234567)
    assert job is not None
    assert job.application_status == "expired"


def test_clean_pending_jobs_respects_max_runtime(tmp_path):
    """A zero-hour budget means the deadline is already past on the first loop
    check, so the sweep stops gracefully before touching any job."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.insert_job(1234567, keyword="test", location_id="test")

    cleaner = JobCleaner(db)
    cleaner.is_job_expired = MagicMock(return_value=True)

    result = cleaner.clean_pending_jobs(max_runtime_hours=0)
    assert result["checked"] == 0
    cleaner.is_job_expired.assert_not_called()


# ---------------------------------------------------------------------------
# Shutdown. The cleaner runs its checks on ThreadPoolExecutor threads, and
# those are NOT daemons: concurrent.futures registers an atexit hook that joins
# them, so anything still queued keeps the whole interpreter alive. A stop that
# the cleaner ignores became "Process finished with exit code -1" minutes later,
# because a batch is up to 500 paced HTTP checks at one worker.
# ---------------------------------------------------------------------------

def _db_with_jobs(tmp_path, count: int) -> DatabaseManager:
    db = DatabaseManager(str(tmp_path / "test.db"))
    for i in range(count):
        db.insert_job(1000 + i, keyword="test", location_id="test")
    return db


def test_a_stop_ends_the_sweep_before_the_next_batch(tmp_path):
    db = _db_with_jobs(tmp_path, 3)
    cleaner = JobCleaner(db, should_stop=lambda: True)
    cleaner.is_job_expired = MagicMock(return_value=True)

    result = cleaner.clean_pending_jobs()

    assert result["checked"] == 0
    cleaner.is_job_expired.assert_not_called()


def test_queued_checks_do_no_work_after_a_stop(tmp_path, monkeypatch):
    """The one that matters, exercised through the real is_job_expired.

    Every queued task is a paced HTTP request on a non-daemon thread. The guard
    lives at the top of is_job_expired, so a test that mocks that method out
    proves nothing — this one leaves it in place and counts network calls.
    """
    import job_search.cleaner.cleaner as cleaner_mod

    monkeypatch.setattr(cleaner_mod.time, "sleep", lambda *_: None)

    db = _db_with_jobs(tmp_path, 60)
    stop = {"now": False}
    session = MagicMock()

    def _get(url, **kwargs):
        if session.get.call_count >= 6:
            stop["now"] = True          # a stop lands part-way through
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "still hiring"
        resp.url = url
        return resp

    session.get.side_effect = _get
    cleaner = JobCleaner(db, session=session, should_stop=lambda: stop["now"])
    cleaner.clean_pending_jobs()

    assert session.get.call_count < 60, (
        "queued checks kept making requests after the stop; those threads are "
        "non-daemon and hold the interpreter open"
    )


def test_a_stop_while_a_task_is_queued_costs_no_request(tmp_path):
    """is_job_expired returns before its pacing sleep and before any HTTP."""
    db = _db_with_jobs(tmp_path, 1)
    session = MagicMock()
    cleaner = JobCleaner(db, session=session, should_stop=lambda: True)

    assert cleaner.is_job_expired(1000) is None
    session.get.assert_not_called()


def test_findings_from_a_stopped_batch_are_still_saved(tmp_path):
    """Stopping should not throw away work already paid for."""
    db = _db_with_jobs(tmp_path, 20)
    stop = {"now": False}
    seen: list[int] = []

    cleaner = JobCleaner(db, should_stop=lambda: stop["now"])

    def _check(job_id: int):
        seen.append(job_id)
        if len(seen) >= 3:
            stop["now"] = True
        return True

    cleaner.is_job_expired = _check
    cleaner.clean_pending_jobs()

    expired = [j for j in seen if db.get_selected_job(j)
               and db.get_selected_job(j).application_status == "expired"]
    assert expired, "jobs checked before the stop should be recorded"


def test_no_should_stop_behaves_as_before(tmp_path):
    db = _db_with_jobs(tmp_path, 2)
    cleaner = JobCleaner(db)
    cleaner.is_job_expired = MagicMock(return_value=True)

    result = cleaner.clean_pending_jobs()
    assert result["checked"] == 2


# ---------------------------------------------------------------------------
# Ordering. The sweep cannot finish inside its runtime budget — thousands of
# jobs at one to two seconds each — so every run is truncated. With a fixed
# order it truncates at the same place every time, which is how newest-first
# left the oldest postings, the likeliest to have expired, never reached.
# ---------------------------------------------------------------------------

def _job_aged(db, job_id: int, days_old: int, cleaned_days_ago=None):
    db.insert_job(job_id, keyword="k", location_id="l")
    with db._cursor() as cur:
        cur.execute(
            "UPDATE jobs SET created_at = datetime('now', ?),"
            "       last_cleaned_at = CASE WHEN ? IS NULL THEN NULL"
            "                              ELSE datetime('now', ?) END"
            " WHERE job_id = ?",
            (f"-{days_old} days", cleaned_days_ago,
             f"-{cleaned_days_ago or 0} days", job_id),
        )


def test_newest_first_is_still_the_default(tmp_path):
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=60)
    _job_aged(db, 2, days_old=1)

    ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner()]
    assert ids == [2, 1]


def test_oldest_first_reverses_it(tmp_path):
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=60)
    _job_aged(db, 2, days_old=1)

    ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner(order="oldest")]
    assert ids == [1, 2], "the old tail must be reachable"


def test_least_checked_puts_never_checked_first(tmp_path):
    """The fair rotation: nothing can be starved by a later run."""
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=10, cleaned_days_ago=4)    # checked recently-ish
    _job_aged(db, 2, days_old=10, cleaned_days_ago=40)   # checked long ago
    _job_aged(db, 3, days_old=10)                        # never checked

    ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner(order="least_checked")]
    assert ids == [3, 2, 1]


def test_selected_only_restricts_the_pile(tmp_path):
    from job_search.core.database import ScreeningResult

    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=5)
    _job_aged(db, 2, days_old=5)
    db.save_screening_result(1, ScreeningResult(0.9, "none", True, "ok", archetype="A"))

    ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner(order="selected")]
    assert ids == [1]


def test_an_unknown_order_falls_back_rather_than_failing(tmp_path):
    """A stale bookmark or hand-edited config must not break the sweep."""
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=60)
    _job_aged(db, 2, days_old=1)

    ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner(order="nonsense")]
    assert ids == [2, 1]


def test_every_order_still_skips_recently_checked_jobs(tmp_path):
    """The interval guard must hold whichever end we work from."""
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=10, cleaned_days_ago=1)     # inside the 3-day window
    _job_aged(db, 2, days_old=10, cleaned_days_ago=10)

    for order in ("newest", "oldest", "least_checked"):
        ids = [j["job_id"] for j in db.get_pending_jobs_for_cleaner(order=order)]
        assert ids == [2], order


def test_the_order_reaches_the_sweep(tmp_path):
    db = DatabaseManager(str(tmp_path / "t.db"))
    _job_aged(db, 1, days_old=60)
    _job_aged(db, 2, days_old=1)

    cleaner = JobCleaner(db)
    cleaner.is_job_expired = MagicMock(return_value=False)
    seen: list[int] = []
    original = db.get_pending_jobs_for_cleaner

    def _spy(*args, **kwargs):
        rows = original(*args, **kwargs)
        seen.extend(r["job_id"] for r in rows)
        return rows

    db.get_pending_jobs_for_cleaner = _spy
    cleaner.clean_pending_jobs(order="oldest")
    assert seen[:2] == [1, 2]
