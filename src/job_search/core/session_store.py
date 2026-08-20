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


# LinkedIn sets its session cookies here. Restoring them under the same domain
# means a Set-Cookie from the server *replaces* our entry instead of sitting
# beside it as a second cookie with the same name.
LINKEDIN_COOKIE_DOMAIN = ".linkedin.com"


def _rank(cookie) -> int:
    """A cookie the server set outranks the bootstrap value we restored."""
    return 1 if cookie.domain else 0


def cookie_values(session_or_jar) -> dict[str, str]:
    """Collapse a cookie jar to one value per name.

    A jar may legitimately hold several cookies with one name under different
    domains, and requests' own ``jar.get(name)`` refuses to choose — it raises
    CookieConflictError. LinkedIn produces exactly that situation: a restored
    session starts with a domainless JSESSIONID, the first response sets
    another for .linkedin.com, and from then on every caller that reads the jar
    by name either crashed or silently picked one at random.

    Server-set cookies win over restored ones, and among those the most
    recently added wins: that is the value LinkedIn treats as current, and the
    CSRF token has to match the JSESSIONID actually being sent.
    """
    jar = getattr(session_or_jar, "cookies", session_or_jar)
    if isinstance(jar, dict):
        return dict(jar)

    chosen: dict[str, object] = {}
    for cookie in jar:
        previous = chosen.get(cookie.name)
        if previous is None or _rank(cookie) >= _rank(previous):
            chosen[cookie.name] = cookie
    return {name: (c.value or "") for name, c in chosen.items()}


def save_session(session: requests.Session, path: str) -> None:
    """Write the cookie jar to disk with owner-only permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": _now_iso(),
        # Not a plain dict comprehension over .items(): with duplicate names
        # that keeps whichever happens to come last, which is how a stale
        # JSESSIONID gets persisted over the live one.
        "cookies": cookie_values(session),
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
        session.cookies.set(name, value, domain=LINKEDIN_COOKIE_DOMAIN, path="/")
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
