"""Tests for verified snapshots and the corruption tripwire.

Written against a real failure: five copies of the database existed, three of
them were the corruption, and nothing had ever checked any of them. So the
properties under test are mostly refusals — refusing to snapshot a damaged
database, refusing to keep a snapshot that failed verification, refusing to
open a damaged file and write migrations into it.
"""
from __future__ import annotations

import sqlite3
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
    """Scribble over the middle of the file, the way a bad copy would."""
    size = path.stat().st_size
    with path.open("r+b") as f:
        f.seek(size // 2)
        f.write(b"\xde\xad\xbe\xef" * 400)


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
