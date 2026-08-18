"""Cross-process run control: locking, stop requests and schedule pausing.

The web UI runner and a scheduled task are separate processes, so an in-process
thread handle cannot coordinate them. These three small files can:

* ``runner.lock``     one run at a time, and identifies who holds it
* ``runner.stop``     a stop request any process can see
* ``schedule.paused`` suppresses scheduled runs until a timestamp

Auto-resume is deliberately lazy. Nothing polls the pause file; the next
scheduled task to start clears it if the resume moment has passed. That means a
laptop asleep through the resume time does not miss it.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger


def _now() -> datetime:
    return datetime.now().astimezone()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Runner lock
# ---------------------------------------------------------------------------

@dataclass
class LockInfo:
    pid: int
    started_at: str
    origin: str          # "manual" | "scheduled"
    stages: str = ""

    @property
    def age_minutes(self) -> float:
        try:
            started = datetime.fromisoformat(self.started_at)
        except ValueError:
            return 0.0
        return (_now() - started).total_seconds() / 60.0


def _pid_alive_windows(pid: int) -> bool:
    """Liveness check for Windows.

    ``os.kill(pid, 0)`` is unusable here: on Windows it routes through
    ``TerminateProcess``, so a live pid would be killed and a dead one raises a
    generic ``OSError`` that is indistinguishable from other failures. We ask
    the OS directly with ``OpenProcess`` instead.
    """
    import ctypes

    ERROR_ACCESS_DENIED = 5
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but is owned by someone else;
        # anything else (typically "invalid parameter") means no such process.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that works on Windows and POSIX."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return True
    return True


def read_lock(path: str) -> LockInfo | None:
    p = Path(path)
    if not p.exists():
        return None
    data = _read_json(p)
    if not data:
        return None
    return LockInfo(
        pid=int(data.get("pid", 0)),
        started_at=str(data.get("started_at", "")),
        origin=str(data.get("origin", "manual")),
        stages=str(data.get("stages", "")),
    )


def is_locked(path: str, stale_after_minutes: int = 120) -> LockInfo | None:
    """Return the holder if a live run owns the lock, else None.

    A lock whose process has died, or which is older than the staleness
    ceiling, is treated as free so a crash cannot block every later run.
    """
    info = read_lock(path)
    if info is None:
        return None
    if not _pid_alive(info.pid):
        # Self-heal: the owner is gone (a crash or a hard kill skipped its
        # release), so delete the orphan instead of only ignoring it. Otherwise
        # every later check re-logs this, and the 2s runner-status poll turns it
        # into a flood. Deleting is safe here precisely because the pid is dead;
        # a live long-running holder is handled by the age branch below, which
        # never deletes.
        logger.info("Removing stale runner lock from dead pid {}", info.pid)
        try:
            Path(path).unlink()
        except OSError:
            pass
        return None
    if info.age_minutes > stale_after_minutes:
        logger.warning(
            "Ignoring runner lock held for {:.0f} min (limit {})",
            info.age_minutes, stale_after_minutes,
        )
        return None
    return info


def acquire_lock(path: str, origin: str, stages: str = "",
                 stale_after_minutes: int = 120) -> bool:
    """Take the lock. Returns False when another live run holds it."""
    if is_locked(path, stale_after_minutes) is not None:
        return False
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "pid": os.getpid(),
        "started_at": _now().isoformat(timespec="seconds"),
        "origin": origin,
        "stages": stages,
    }, indent=2), encoding="utf-8")
    return True


def release_lock(path: str) -> None:
    p = Path(path)
    if not p.exists():
        return
    info = read_lock(path)
    # Only drop our own lock, so a force-stop cannot be undone by a late exit.
    if info is not None and info.pid not in (0, os.getpid()):
        return
    try:
        p.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Exclusive short-lived lock
# ---------------------------------------------------------------------------

def _claim_exclusive(p: Path, origin: str) -> bool:
    """Create the lock file, failing if someone else got there first.

    ``acquire_lock`` above reads and then writes, which leaves a window two
    processes starting in the same second can both pass. O_CREAT|O_EXCL closes
    it: the existence check and the creation are one syscall, on Windows as
    well as POSIX. That matters here because the racing callers are woken by
    the same event (a laptop coming back from sleep firing every missed task at
    once), so they arrive within milliseconds of each other.
    """
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, json.dumps({
            "pid": os.getpid(),
            "started_at": _now().isoformat(timespec="seconds"),
            "origin": origin,
        }, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _exclusive_is_stale(p: Path, stale_after_minutes: int) -> bool:
    """True when the lock file is left over rather than genuinely held."""
    info = read_lock(str(p))
    if info is None:
        return True                       # unreadable or truncated
    if not _pid_alive(info.pid):
        return True
    return info.age_minutes > stale_after_minutes


@contextmanager
def exclusive(path: str, stale_after_minutes: int = 30, origin: str = ""):
    """Hold a cross-process lock for the duration of the block.

    Yields True when this process owns it and False when another live process
    does. Callers are expected to skip their work on False rather than wait:
    every user of this is idempotent and will run again shortly.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    held = _claim_exclusive(p, origin)
    if not held and _exclusive_is_stale(p, stale_after_minutes):
        logger.info("Removing stale lock {}", p.name)
        try:
            p.unlink()
        except OSError:
            pass
        held = _claim_exclusive(p, origin)

    try:
        yield held
    finally:
        if held:
            # release_lock only removes a file this pid owns, so a lock we lost
            # to a staleness sweep is not deleted out from under its new owner.
            release_lock(str(p))


# ---------------------------------------------------------------------------
# Stop requests
# ---------------------------------------------------------------------------

def request_stop(path: str, reason: str = "requested from web UI") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "requested_at": _now().isoformat(timespec="seconds"),
        "reason": reason,
    }, indent=2), encoding="utf-8")
    logger.info("Stop requested: {}", reason)


def stop_requested(path: str) -> bool:
    return Path(path).exists()


def clear_stop(path: str) -> None:
    p = Path(path)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Schedule pause, with lazy auto-resume
# ---------------------------------------------------------------------------

PAUSE_PRESETS = ("12h", "tomorrow_morning", "24h", "indefinite")


def resolve_resume_at(preset: str, morning_hour: int = 7) -> datetime | None:
    """Turn a preset into an absolute timestamp at the moment of pausing.

    Resolving now rather than at read time means a clock change or DST shift
    cannot move the resume moment.
    """
    now = _now()
    if preset == "12h":
        return now + timedelta(hours=12)
    if preset == "24h":
        return now + timedelta(hours=24)
    if preset == "tomorrow_morning":
        candidate = now.replace(hour=morning_hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return None          # indefinite


def pause_schedule(path: str, preset: str = "tomorrow_morning",
                   morning_hour: int = 7, reason: str = "manual pause") -> datetime | None:
    resume_at = resolve_resume_at(preset, morning_hour)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "paused_at": _now().isoformat(timespec="seconds"),
        "resume_at": resume_at.isoformat(timespec="seconds") if resume_at else None,
        "preset": preset,
        "reason": reason,
    }, indent=2), encoding="utf-8")
    logger.info("Schedule paused until {}", resume_at or "resumed manually")
    return resume_at


def resume_schedule(path: str) -> None:
    p = Path(path)
    if p.exists():
        try:
            p.unlink()
            logger.info("Schedule resumed")
        except OSError:
            pass


def pause_state(path: str) -> dict | None:
    """Return the active pause, or None. Clears the file if it has expired.

    This is the auto-resume: the first scheduled task to look after the resume
    moment removes the pause and proceeds.
    """
    p = Path(path)
    if not p.exists():
        return None
    data = _read_json(p)
    if data is None:
        resume_schedule(path)
        return None

    raw = data.get("resume_at")
    if not raw:
        return data          # indefinite

    try:
        resume_at = datetime.fromisoformat(raw)
    except ValueError:
        resume_schedule(path)
        return None

    if _now() >= resume_at:
        logger.info("Schedule auto-resumed (pause expired at {})", raw)
        resume_schedule(path)
        return None

    data["_resume_at"] = resume_at
    return data


def pause_remaining(path: str) -> str | None:
    """Human phrasing for the UI, e.g. "9h 20m"."""
    state = pause_state(path)
    if state is None:
        return None
    resume_at = state.get("_resume_at")
    if resume_at is None:
        return "indefinitely"
    delta = resume_at - _now()
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"
