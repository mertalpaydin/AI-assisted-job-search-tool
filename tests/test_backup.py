"""Tests for verified snapshots and the corruption tripwire.

Written against a real failure: five copies of the database existed, three of
them were the corruption, and nothing had ever checked any of them. So the
properties under test are mostly refusals — refusing to snapshot a damaged
database, refusing to keep a snapshot that failed verification, refusing to
open a damaged file and write migrations into it.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from job_search.core.backup import (
    DatabaseCorruptError,
    IdleSnapshotter,
    Snapshot,
    SnapshotManager,
    assert_healthy,
    quick_check,
)
from job_search.core.database import DatabaseManager


def _make_db(path: Path, rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _corrupt(path: Path) -> None:
    """Scribble over the middle half of the file, the way a bad copy would.

    A region rather than a single 1600-byte spot, because one spot lands
    wherever the current schema happens to put it. Adding three columns to
    ``jobs`` was enough to move the midpoint onto a page that schema setup
    never reads, so the file was genuinely corrupt and opening it still
    succeeded — the tripwire below passed for a reason that had nothing to do
    with the tripwire. Damaging the whole middle keeps every one of these
    tests independent of the byte layout.
    """
    size = path.stat().st_size
    start, end = size // 4, (size * 3) // 4
    with path.open("r+b") as f:
        f.seek(start)
        f.write(b"\xde\xad\xbe\xef" * max(1, (end - start) // 4))


class TestIntegrityCheck:
    def test_a_healthy_database_passes(self, tmp_path: Path) -> None:
        db = tmp_path / "ok.db"
        _make_db(db)
        assert quick_check(db) is None
        assert_healthy(db)                      # does not raise

    def test_a_missing_file_is_not_corruption(self, tmp_path: Path) -> None:
        """A first run has no database yet; that must not look like damage."""
        assert quick_check(tmp_path / "nope.db") is None

    def test_damage_is_detected(self, tmp_path: Path) -> None:
        db = tmp_path / "bad.db"
        _make_db(db, rows=2000)
        _corrupt(db)
        assert quick_check(db) is not None

    def test_assert_healthy_names_the_recovery_command(self, tmp_path: Path) -> None:
        db = tmp_path / "bad.db"
        _make_db(db, rows=2000)
        _corrupt(db)
        with pytest.raises(DatabaseCorruptError, match="job-search restore"):
            assert_healthy(db)


class TestTripwire:
    def test_opening_a_damaged_database_is_refused(self, tmp_path: Path) -> None:
        """Every open runs migrations, so every open writes. Stop before that."""
        db = tmp_path / "jobs.db"
        DatabaseManager(str(db)).close()
        _corrupt(db)

        with pytest.raises(DatabaseCorruptError):
            DatabaseManager(str(db))

    def test_without_the_check_you_get_sqlite_s_bare_error(self, tmp_path: Path) -> None:
        """Skipping the check does not make a corrupt file usable.

        All it costs you is the diagnosis: SQLite raises somewhere inside
        schema setup with "database disk image is malformed" and no hint that
        a verified snapshot is one command away. That bare error is exactly
        what sent us hunting through five copies.
        """
        db = tmp_path / "jobs.db"
        DatabaseManager(str(db)).close()
        _corrupt(db)

        with pytest.raises(sqlite3.DatabaseError) as raw:
            DatabaseManager(str(db), check_integrity=False)
        assert not isinstance(raw.value, DatabaseCorruptError)

        with pytest.raises(DatabaseCorruptError, match="restore latest"):
            DatabaseManager(str(db))


class TestSnapshots:
    def test_a_snapshot_is_taken_and_verified(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")

        snap = m.take("test")
        assert snap is not None
        assert snap.path.exists()
        assert quick_check(snap.path) is None
        assert "test" in snap.path.name

    def test_a_damaged_database_is_never_snapshotted(self, tmp_path: Path) -> None:
        """Otherwise the good snapshot gets rotated out by a bad one."""
        db = tmp_path / "jobs.db"
        _make_db(db, rows=2000)
        m = SnapshotManager(db, tmp_path / "backups")
        assert m.take("good") is not None

        _corrupt(db)
        assert m.take("after-damage") is None
        assert len(m.list()) == 1
        assert m.list()[0].reason == "good"

    def test_no_partial_files_are_left_behind(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        m.take("test")
        assert list((tmp_path / "backups").glob("*.partial")) == []

    def test_snapshotting_a_missing_database_is_a_no_op(self, tmp_path: Path) -> None:
        m = SnapshotManager(tmp_path / "absent.db", tmp_path / "backups")
        assert m.take() is None


class TestRetention:
    def _seed(self, directory: Path, ages_hours: list[float]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        for h in ages_hours:
            stamp = (now - timedelta(hours=h)).strftime("%Y%m%d-%H%M%S")
            (directory / f"jobs-{stamp}-run.db").write_bytes(b"x")

    def test_keeps_only_the_configured_number(self, tmp_path: Path) -> None:
        d = tmp_path / "backups"
        self._seed(d, [0, 1, 2, 20, 200, 400])
        m = SnapshotManager(tmp_path / "jobs.db", d, keep=3)
        m.prune()
        assert len(m.list()) == 3

    def test_the_slots_spread_across_time(self, tmp_path: Path) -> None:
        """Three snapshots twenty minutes apart cover twenty minutes.

        The tiers exist so the same three files span a week instead — which is
        what protects against damage noticed late, or a bad edit that every
        integrity check in the world will call healthy.
        """
        d = tmp_path / "backups"
        self._seed(d, [0, 0.5, 1, 20, 200])
        m = SnapshotManager(tmp_path / "jobs.db", d, keep=3, tier_hours=(0, 12, 168))
        m.prune()

        ages = sorted(round(s.age.total_seconds() / 3600) for s in m.list())
        assert ages[0] == 0            # newest
        assert 12 <= ages[1] < 168     # the ~12h tier
        assert ages[2] >= 168          # the ~1 week tier

    def test_a_fresh_install_just_keeps_the_newest(self, tmp_path: Path) -> None:
        """With no history yet the tiers have no candidates; don't delete everything."""
        d = tmp_path / "backups"
        self._seed(d, [0, 0.2, 0.4, 0.6])
        m = SnapshotManager(tmp_path / "jobs.db", d, keep=3)
        m.prune()
        assert len(m.list()) == 3

    def test_pruning_is_a_no_op_below_the_limit(self, tmp_path: Path) -> None:
        d = tmp_path / "backups"
        self._seed(d, [0, 5])
        m = SnapshotManager(tmp_path / "jobs.db", d, keep=3)
        assert m.prune() == []


class TestRestore:
    def test_restore_swaps_in_the_snapshot_and_keeps_the_old_file(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db, rows=3)
        m = SnapshotManager(db, tmp_path / "backups")
        m.take("before")

        conn = sqlite3.connect(db)          # diverge from the snapshot
        conn.execute("INSERT INTO t (v) VALUES ('later')")
        conn.commit()
        conn.close()

        m.restore("latest")

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
        conn.close()
        assert list(tmp_path.glob("jobs.db.replaced-*")), "old database must be kept"

    def test_a_damaged_snapshot_is_refused(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db, rows=2000)
        m = SnapshotManager(db, tmp_path / "backups")
        snap = m.take("x")
        _corrupt(snap.path)

        with pytest.raises(DatabaseCorruptError):
            m.restore("latest")

    def test_restoring_with_no_snapshots_fails_clearly(self, tmp_path: Path) -> None:
        m = SnapshotManager(tmp_path / "jobs.db", tmp_path / "backups")
        with pytest.raises(FileNotFoundError):
            m.restore()

    def test_stale_journal_files_do_not_survive_a_restore(self, tmp_path: Path) -> None:
        """They belong to the file being moved aside, not the one coming in."""
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        m.take("x")
        db.with_name("jobs.db-wal").write_bytes(b"stale")
        db.with_name("jobs.db-shm").write_bytes(b"stale")

        m.restore("latest")
        assert not db.with_name("jobs.db-wal").exists()
        assert not db.with_name("jobs.db-shm").exists()


class TestIdleSnapshotter:
    """The web UI writes the data that cannot be re-derived by any run."""

    def test_nothing_is_taken_while_the_user_is_still_clicking(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"), idle_seconds=60)
        s.mark_dirty()
        assert s._due() is False

    def test_a_snapshot_is_due_once_the_writes_stop(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"), idle_seconds=0)
        s.mark_dirty()
        assert s._due() is True

    def test_a_long_unbroken_session_still_gets_one(self, tmp_path: Path) -> None:
        """Someone working steadily for an hour never goes idle."""
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"),
                            idle_seconds=10_000, max_interval_seconds=0)
        s.mark_dirty()
        assert s._due() is True

    def test_reads_alone_never_trigger_a_snapshot(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"), idle_seconds=0)
        assert s._due() is False
        assert s.flush() is None

    def test_flush_captures_pending_work_on_shutdown(self, tmp_path: Path) -> None:
        """A browsing session usually ends by closing the window."""
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        s = IdleSnapshotter(m, idle_seconds=10_000)
        s.mark_dirty()

        assert s.flush() is not None
        assert len(m.list()) == 1
        assert s.flush() is None            # nothing pending the second time


class TestShutdownIsFast:
    """A VACUUM during interpreter shutdown got the process killed.

    6-11s on a 265MB database, run from an atexit hook, meant an IDE's stop
    button terminated it mid-write: exit code -1, no snapshot, and a
    quarter-gigabyte .partial left behind. Coverage moved to startup instead.
    """

    def test_a_stale_database_is_detected_at_startup(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        m.take("first")
        assert m.is_stale() is False

        conn = sqlite3.connect(db)                  # the edit a session ended on
        conn.execute("INSERT INTO t (v) VALUES ('after')")
        conn.commit()
        conn.close()
        assert m.is_stale() is True

    def test_catch_up_queues_a_snapshot_without_taking_one(self, tmp_path: Path) -> None:
        """Startup must not block on a VACUUM either — just mark it due."""
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        s = IdleSnapshotter(m, idle_seconds=0, min_interval_seconds=0)

        s.catch_up()
        assert m.list() == []                       # nothing written yet
        assert s._due() is True                     # but the thread will

    def test_no_snapshots_at_all_counts_as_stale(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        assert SnapshotManager(db, tmp_path / "backups").is_stale() is True

    def test_a_missing_database_is_not_stale(self, tmp_path: Path) -> None:
        assert SnapshotManager(tmp_path / "gone.db", tmp_path / "backups").is_stale() is False


class TestSnapshotChurn:
    """One hour of browsing produced eleven full-database VACUUMs."""

    def test_a_second_snapshot_is_refused_too_soon(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"),
                            idle_seconds=0, min_interval_seconds=900)
        s.mark_dirty()
        assert s._due() is True
        s.flush()                                   # sets the floor

        s.mark_dirty()
        assert s._due() is False, "should wait out min_interval_seconds"

    def test_the_floor_can_be_disabled(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        s = IdleSnapshotter(SnapshotManager(db, tmp_path / "backups"),
                            idle_seconds=0, min_interval_seconds=0)
        s.mark_dirty()
        s.flush()
        s.mark_dirty()
        assert s._due() is True

    def test_waiting_loses_nothing(self, tmp_path: Path) -> None:
        """A snapshot copies the whole database, so a later one still covers
        every edit the skipped one would have."""
        db = tmp_path / "jobs.db"
        _make_db(db, rows=2)
        m = SnapshotManager(db, tmp_path / "backups")
        s = IdleSnapshotter(m, idle_seconds=0, min_interval_seconds=0)

        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO t (v) VALUES ('skipped-window')")
        conn.commit()
        conn.close()
        s.mark_dirty()
        snap = s.flush()

        check = sqlite3.connect(f"file:{snap.path}?mode=ro", uri=True)
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3


class TestAbandonedPartials:
    def test_an_old_partial_is_swept(self, tmp_path: Path) -> None:
        """A killed VACUUM leaves a file the size of the whole database."""
        backups = tmp_path / "backups"
        backups.mkdir()
        orphan = backups / "jobs-20260820-141350-webui.db.partial"
        orphan.write_bytes(b"x" * 1024)
        os.utime(orphan, (time.time() - 86400, time.time() - 86400))

        SnapshotManager(tmp_path / "jobs.db", backups).sweep_partials()
        assert not orphan.exists()

    def test_a_vacuum_in_flight_is_left_alone(self, tmp_path: Path) -> None:
        backups = tmp_path / "backups"
        backups.mkdir()
        live = backups / "jobs-now-webui.db.partial"
        live.write_bytes(b"x")

        SnapshotManager(tmp_path / "jobs.db", backups).sweep_partials()
        assert live.exists(), "a partial written seconds ago may still be growing"

    def test_taking_a_snapshot_sweeps_them(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        backups = tmp_path / "backups"
        backups.mkdir()
        orphan = backups / "jobs-old-run.db.partial"
        orphan.write_bytes(b"x")
        os.utime(orphan, (time.time() - 86400, time.time() - 86400))

        SnapshotManager(db, backups).take("x")
        assert not orphan.exists()


class TestOffsite:
    def test_compressed_copy_uses_a_stable_name(self, tmp_path: Path) -> None:
        """A stable path is what gives a sync provider a version history."""
        db = tmp_path / "jobs.db"
        _make_db(db, rows=500)
        target = tmp_path / "synced" / "jobs-latest.db.gz"
        m = SnapshotManager(db, tmp_path / "backups", offsite=target)

        first = m.take("one")
        m.push_offsite(first)
        assert target.exists()
        size_one = target.stat().st_size

        second = m.take("two")
        m.push_offsite(second)
        assert target.exists()               # same filename, overwritten
        assert size_one > 0

    def test_no_offsite_configured_is_a_no_op(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        _make_db(db)
        m = SnapshotManager(db, tmp_path / "backups")
        m.take("x")
        assert m.push_offsite() is None
