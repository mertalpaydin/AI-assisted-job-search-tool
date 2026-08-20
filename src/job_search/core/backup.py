"""Verified database snapshots, and the tripwire that catches a broken one.

Written after a corruption incident where five copies of the database existed
and three of them were the corruption, because nothing had ever checked them.
Two rules come out of that:

* **Snapshot with VACUUM INTO, never a file copy.** It writes a transactionally
  consistent image in well under a second. Any byte-level copier — a sync
  client, an explorer drag, ``shutil.copy`` — reads ``jobs.db``, ``-wal`` and
  ``-shm`` at different moments while they are being written, and can produce a
  file that looks fine and is not.
* **Verify before storing, and again before restoring.** A snapshot that has
  not passed ``quick_check`` is not a backup. Because every retained snapshot
  is verified, three of them are worth more than five unverified ones, and
  "all my backups are damaged" stops being possible.

The tripwire matters as much as the snapshots. Opening this database runs
migrations, so *every* open is a write: without a check, a damaged file quietly
accumulates more damage every time anything touches it.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

_STAMP = "%Y%m%d-%H%M%S"


class DatabaseCorruptError(RuntimeError):
    """The database failed its integrity check."""


def quick_check(path: str | Path) -> str | None:
    """Return None when the database is healthy, else the first failure line.

    Costs ~0.3s on a 240MB database, which is cheap enough to run on every
    open. A missing file is not corruption — a fresh database is created on
    first use.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"could not open: {exc}"
    try:
        rows = conn.execute("PRAGMA quick_check(1)").fetchall()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()
    if rows and str(rows[0][0]).strip().lower() == "ok":
        return None
    return str(rows[0][0]).splitlines()[0] if rows else "unknown failure"


def assert_healthy(path: str | Path) -> None:
    """Refuse to go any further with a damaged database.

    Raised rather than logged: the caller is about to run migrations, and
    writing into a corrupt file is how a bad afternoon becomes a bad week.
    """
    failure = quick_check(path)
    if failure is None:
        return
    raise DatabaseCorruptError(
        f"{path} failed its integrity check: {failure}\n"
        f"Nothing has been written. Restore a verified snapshot with:\n"
        f"    uv run job-search restore latest"
    )


@dataclass(frozen=True)
class Snapshot:
    path: Path
    taken_at: datetime
    reason: str

    @property
    def size_mb(self) -> float:
        return self.path.stat().st_size / 1e6

    @property
    def age(self) -> timedelta:
        return datetime.now() - self.taken_at


def _parse(path: Path) -> Snapshot | None:
    """jobs-20260820-134233-webui.db -> Snapshot."""
    stem = path.name[len("jobs-"):].removesuffix(".db")
    parts = stem.split("-")
    if len(parts) < 2:
        return None
    try:
        taken = datetime.strptime(f"{parts[0]}-{parts[1]}", _STAMP)
    except ValueError:
        return None
    return Snapshot(path=path, taken_at=taken, reason="-".join(parts[2:]) or "manual")


class SnapshotManager:
    """Takes, prunes, lists and restores verified snapshots."""

    def __init__(self, db_path: str | Path, directory: str | Path = "data/backups",
                 keep: int = 3, tier_hours: tuple[int, ...] = (0, 12, 168),
                 offsite: str | Path | None = None) -> None:
        self._db = Path(db_path)
        self._dir = Path(directory)
        self._keep = max(1, keep)
        self._tiers = tier_hours
        self._offsite = Path(offsite) if offsite else None

    # ------------------------------------------------------------------

    def take(self, reason: str = "manual") -> Snapshot | None:
        """Snapshot the database, but only if it is healthy, and verify the result."""
        if not self._db.exists():
            return None

        failure = quick_check(self._db)
        if failure is not None:
            # Storing this would overwrite a good snapshot with a bad one.
            logger.error("Refusing to snapshot a damaged database: {}", failure)
            return None

        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime(_STAMP)
        target = self._dir / f"jobs-{stamp}-{reason}.db"
        partial = target.with_suffix(".db.partial")

        started = time.monotonic()
        try:
            conn = sqlite3.connect(str(self._db))
            try:
                partial.unlink(missing_ok=True)
                conn.execute("VACUUM INTO ?", (str(partial),))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Snapshot failed: {}", exc)
            partial.unlink(missing_ok=True)
            return None

        # Verify what was actually written, not what we hoped was written.
        failure = quick_check(partial)
        if failure is not None:
            logger.error("Discarding snapshot that failed verification: {}", failure)
            partial.unlink(missing_ok=True)
            return None

        partial.replace(target)
        snap = Snapshot(target, datetime.now(), reason)
        logger.info("Snapshot {} ({:.0f}MB, {:.1f}s, reason={})",
                    target.name, snap.size_mb, time.monotonic() - started, reason)
        self.prune()
        return snap

    def list(self) -> list[Snapshot]:
        """Newest first."""
        if not self._dir.exists():
            return []
        found = [s for s in (_parse(p) for p in self._dir.glob("jobs-*.db")) if s]
        return sorted(found, key=lambda s: s.taken_at, reverse=True)

    # ------------------------------------------------------------------

    def _select_keep(self, snaps: list[Snapshot]) -> list[Snapshot]:
        """Spread the slots over time instead of over the last few hours.

        Three snapshots taken twenty minutes apart cover twenty minutes. The
        tiers push the older slots backwards so the same three files span a
        week, which is what protects against damage noticed late — or against
        a bad edit, which passes every integrity check ever written.

        Slots with no candidate yet fall back to the next newest, so a fresh
        install keeps its three most recent and spreads out as history builds.
        """
        chosen: list[Snapshot] = []
        previous: Snapshot | None = None
        for gap in self._tiers[:self._keep]:
            candidate = next(
                (s for s in snaps
                 if s not in chosen
                 and (previous is None
                      or (previous.taken_at - s.taken_at) >= timedelta(hours=gap))),
                None,
            )
            if candidate is not None:
                chosen.append(candidate)
                previous = candidate
        for s in snaps:                      # fill any empty slots
            if len(chosen) >= self._keep:
                break
            if s not in chosen:
                chosen.append(s)
        return chosen[:self._keep]

    def prune(self) -> list[Path]:
        snaps = self.list()
        if len(snaps) <= self._keep:
            return []
        keep = set(self._select_keep(snaps))
        removed = []
        for s in snaps:
            if s not in keep:
                try:
                    s.path.unlink()
                    removed.append(s.path)
                except OSError as exc:
                    logger.warning("Could not remove old snapshot {}: {}", s.path.name, exc)
        if removed:
            logger.debug("Pruned {} old snapshot(s)", len(removed))
        return removed

    # ------------------------------------------------------------------

    def push_offsite(self, snapshot: Snapshot | None = None) -> Path | None:
        """Compress the newest snapshot to a stable filename.

        A stable name is the point: a sync client keeps version history per
        path, so overwriting one file gives you a rolling history off the
        machine, where timestamped names would just pile up. It compresses to
        roughly a quarter of the raw size, which keeps the upload sane.
        """
        if self._offsite is None:
            return None
        snapshot = snapshot or next(iter(self.list()), None)
        if snapshot is None:
            return None

        self._offsite.parent.mkdir(parents=True, exist_ok=True)
        partial = self._offsite.with_suffix(self._offsite.suffix + ".partial")
        started = time.monotonic()
        try:
            with snapshot.path.open("rb") as src, gzip.open(partial, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            partial.replace(self._offsite)
        except OSError as exc:
            logger.warning("Offsite copy failed: {}", exc)
            partial.unlink(missing_ok=True)
            return None
        logger.info("Offsite copy {} ({:.0f}MB, {:.0f}s)", self._offsite.name,
                    self._offsite.stat().st_size / 1e6, time.monotonic() - started)
        return self._offsite

    def restore(self, name: str = "latest") -> Path:
        """Put a verified snapshot back in place, keeping the current file."""
        snaps = self.list()
        if not snaps:
            raise FileNotFoundError(f"No snapshots in {self._dir}")

        if name in ("latest", "", None):
            chosen = snaps[0]
        else:
            chosen = next((s for s in snaps if s.path.name in (name, f"{name}.db")), None)
            if chosen is None:
                raise FileNotFoundError(f"No snapshot named {name}")

        failure = quick_check(chosen.path)
        if failure is not None:
            raise DatabaseCorruptError(f"Snapshot {chosen.path.name} is damaged: {failure}")

        if self._db.exists():
            aside = self._db.with_name(
                f"{self._db.name}.replaced-{datetime.now().strftime(_STAMP)}")
            self._db.replace(aside)
            logger.info("Previous database kept as {}", aside.name)
        # Stale journal files belong to the file we just moved aside.
        for suffix in ("-wal", "-shm"):
            self._db.with_name(self._db.name + suffix).unlink(missing_ok=True)

        shutil.copy2(chosen.path, self._db)
        failure = quick_check(self._db)
        if failure is not None:                      # pragma: no cover - paranoia
            raise DatabaseCorruptError(f"Restored file failed verification: {failure}")
        logger.info("Restored {} -> {}", chosen.path.name, self._db)
        return self._db


class IdleSnapshotter:
    """Snapshots a burst of interactive edits once the user stops making them.

    The web UI is not a read-only viewer: applying, skipping, approving and
    note-taking all happen there, and that is the data no scrape or screening
    run can ever reproduce. But a snapshot per click would be absurd, so writes
    only mark the database dirty and a background thread waits for quiet.

    A ceiling covers the other case — someone working steadily for an hour
    without ever pausing long enough to trigger the idle path.
    """

    def __init__(self, manager: SnapshotManager, idle_seconds: float = 120.0,
                 max_interval_seconds: float = 1800.0, reason: str = "webui") -> None:
        self._manager = manager
        self._idle = idle_seconds
        self._max_interval = max_interval_seconds
        self._reason = reason
        self._lock = threading.Lock()
        self._dirty_since: float | None = None
        self._last_write: float = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def mark_dirty(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._last_write = now
            if self._dirty_since is None:
                self._dirty_since = now

    def _due(self) -> bool:
        with self._lock:
            if self._dirty_since is None:
                return False
            now = time.monotonic()
            return (now - self._last_write >= self._idle
                    or now - self._dirty_since >= self._max_interval)

    def _clear(self) -> None:
        with self._lock:
            self._dirty_since = None

    def flush(self) -> Snapshot | None:
        """Snapshot now if anything is pending. Used on shutdown."""
        with self._lock:
            pending = self._dirty_since is not None
        if not pending:
            return None
        self._clear()
        return self._manager.take(self._reason)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="snapshotter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(10.0):
            if self._due():
                self._clear()
                try:
                    self._manager.take(self._reason)
                except Exception as exc:          # never take the UI down
                    logger.warning("Idle snapshot failed: {}", exc)
