"""Tests for reading a cookie jar that holds duplicate names.

A restored session sets its cookies without a domain; LinkedIn's first
response then sets JSESSIONID for .linkedin.com. requests keeps both, because
they differ by domain, and from that moment ``jar.get("JSESSIONID")`` raises
CookieConflictError. That took the scraping workers down mid-run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests
from requests.cookies import CookieConflictError

from job_search.core.session_store import (
    LINKEDIN_COOKIE_DOMAIN,
    cookie_values,
    load_session,
    save_session,
)
from job_search.scraping.auth import make_headers


def _jar_with_duplicates() -> requests.Session:
    """Exactly the shape that broke: a restored cookie, then the server's."""
    s = requests.Session()
    s.cookies.set("JSESSIONID", '"ajax:restored"')          # domainless
    s.cookies.set("JSESSIONID", '"ajax:from-server"',
                  domain=".linkedin.com", path="/")
    s.cookies.set("li_at", "token", domain=".linkedin.com", path="/")
    return s


class TestTheFailure:
    def test_requests_own_get_still_raises(self) -> None:
        """Guards the premise: this is why we cannot just call jar.get()."""
        s = _jar_with_duplicates()
        with pytest.raises(CookieConflictError):
            s.cookies.get("JSESSIONID")

    def test_make_headers_survives_it(self) -> None:
        headers = make_headers(_jar_with_duplicates())
        assert headers["Csrf-Token"] == "ajax:from-server"

    def test_the_cookie_header_never_repeats_a_name(self) -> None:
        """Two JSESSIONIDs in one Cookie header is its own kind of broken."""
        cookie_header = make_headers(_jar_with_duplicates())["Cookie"]
        assert cookie_header.count("JSESSIONID=") == 1
        assert "ajax:from-server" in cookie_header

    def test_the_csrf_token_matches_the_cookie_that_is_sent(self) -> None:
        """They must agree or LinkedIn rejects the request."""
        headers = make_headers(_jar_with_duplicates())
        assert f'JSESSIONID="{headers["Csrf-Token"]}"' in headers["Cookie"]


class TestPrecedence:
    def test_the_server_cookie_beats_the_restored_one(self) -> None:
        assert cookie_values(_jar_with_duplicates())["JSESSIONID"] == '"ajax:from-server"'

    def test_the_newest_server_cookie_wins(self) -> None:
        """LinkedIn rotates JSESSIONID; the latest is the live one."""
        s = requests.Session()
        s.cookies.set("JSESSIONID", "old", domain=".linkedin.com", path="/old")
        s.cookies.set("JSESSIONID", "new", domain=".www.linkedin.com", path="/")
        assert cookie_values(s)["JSESSIONID"] == "new"

    def test_a_plain_dict_passes_through(self) -> None:
        assert cookie_values({"a": "1"}) == {"a": "1"}

    def test_an_empty_jar_is_empty(self) -> None:
        assert cookie_values(requests.Session()) == {}


class TestPersistence:
    def test_a_stale_duplicate_is_not_what_gets_saved(self, tmp_path: Path) -> None:
        """A dict comprehension over .items() would persist the wrong one."""
        path = tmp_path / "session.json"
        save_session(_jar_with_duplicates(), str(path))

        stored = json.loads(path.read_text(encoding="utf-8"))["cookies"]
        assert stored["JSESSIONID"] == '"ajax:from-server"'

    def test_restored_cookies_carry_the_linkedin_domain(self, tmp_path: Path) -> None:
        """Without a domain, the server's Set-Cookie adds a second entry
        instead of replacing this one — which is how the duplicate arose."""
        path = tmp_path / "session.json"
        path.write_text(json.dumps({"saved_at": "now", "cookies": {
            "li_at": "token", "JSESSIONID": '"ajax:x"', "bcookie": "b",
        }}), encoding="utf-8")

        session = load_session(str(path))
        assert session is not None
        assert {c.domain for c in session.cookies} == {LINKEDIN_COOKIE_DOMAIN}

    def test_a_round_trip_leaves_one_cookie_per_name(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        save_session(_jar_with_duplicates(), str(path))
        restored = load_session(str(path))

        assert restored is not None
        names = [c.name for c in restored.cookies]
        assert len(names) == len(set(names))
        assert restored.cookies.get("JSESSIONID") == '"ajax:from-server"'
