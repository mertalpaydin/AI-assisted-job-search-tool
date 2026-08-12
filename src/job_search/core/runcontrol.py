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


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that works on Windows and POSIX."""
    if pid <= 0:
        return False
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
        logger.info("Ignoring stale runner lock from dead pid {}", info.pid)
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
