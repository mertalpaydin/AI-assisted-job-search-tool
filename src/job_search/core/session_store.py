"""Persist and validate the authenticated LinkedIn session.

Before this existed, every run drove a full Selenium login, which with 2FA
enabled meant every run demanded a phone approval. Scheduling was therefore
impossible.

After login the scraper never touches Selenium again: search and details both
issue plain `requests` calls. So persisting the cookie jar is enough to run
unattended until LinkedIn invalidates it, which for a remembered login is
typically months.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import requests
from loguru import logger

# One cheap authenticated call used to decide whether a restored session works.
_VALIDATION_URL = (
    "https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards"
    "?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-187"
    "&count=1&q=jobSearch"
    "&query=(origin:JOB_SEARCH_PAGE_OTHER_ENTRY,keywords:test,spellCorrectionEnabled:true)"
    "&start=0"
)

# Cookies worth keeping. li_at is the session; JSESSIONID carries the CSRF token.
_ESSENTIAL_COOKIES = ("li_at", "JSESSIONID")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_session(session: requests.Session, path: str) -> None:
    """Write the cookie jar to disk with owner-only permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": _now_iso(),
        "cookies": {k: v for k, v in session.cookies.items()},
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        # Best effort: the file grants access to the account, so keep it private.
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    logger.debug("LinkedIn session saved to {}", p)


def load_session(path: str) -> requests.Session | None:
    """Rebuild a requests.Session from disk, or None if unusable."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Stored LinkedIn session unreadable ({}), ignoring", exc)
        return None

    cookies = payload.get("cookies") or {}
    missing = [c for c in _ESSENTIAL_COOKIES if not cookies.get(c)]
    if missing:
        logger.warning("Stored session missing {}, ignoring", ", ".join(missing))
        return None

    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value)
    return session


def session_saved_at(path: str) -> str | None:
    """Return the ISO timestamp the session was stored, for the UI."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("saved_at")
    except (json.JSONDecodeError, OSError):
        return None


def validate_session(session: requests.Session, timeout: int = 15) -> bool:
    """Return True when the session is still authenticated.

    One request. Anything other than a clean 200 is treated as invalid, which
    is deliberately strict: a scheduled run should skip LinkedIn rather than
    hammer it with a session that might be half-dead.
    """
    from job_search.scraping.auth import make_headers

    try:
        resp = session.get(_VALIDATION_URL, headers=make_headers(session), timeout=timeout)
    except requests.RequestException as exc:
        logger.warning("Session validation failed to reach LinkedIn: {}", exc)
        return False

    if resp.status_code == 200:
        return True
    if resp.status_code in (401, 403):
        logger.info("Stored LinkedIn session rejected (HTTP {})", resp.status_code)
    else:
        logger.warning("Session validation returned HTTP {}", resp.status_code)
    return False


def clear_session(path: str) -> None:
    """Delete the stored session, forcing a fresh login next time."""
    p = Path(path)
    if p.exists():
        p.unlink()
        logger.info("Stored LinkedIn session cleared")
