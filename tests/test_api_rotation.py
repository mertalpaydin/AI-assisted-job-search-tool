"""Tests for job_search.utils.api_rotation — GeminiAPIRotator."""
from __future__ import annotations

import time

import pytest

from job_search.utils.api_rotation import GeminiAPIRotator


class TestGeminiAPIRotator:
    def test_requires_at_least_one_key(self) -> None:
        with pytest.raises(ValueError):
            GeminiAPIRotator([])

    def test_returns_key_and_index(self) -> None:
        rotator = GeminiAPIRotator(["key-a"], requests_per_minute=60)
        idx, key = rotator.get_next_available_key()
        assert idx == 0
        assert key == "key-a"

    def test_round_robin_across_keys(self) -> None:
        rotator = GeminiAPIRotator(["k1", "k2", "k3"], requests_per_minute=60)
        indices = [rotator.get_next_available_key()[0] for _ in range(6)]
        # Should cycle 0->1->2->0->1->2
        assert indices == [0, 1, 2, 0, 1, 2]

    def test_record_success_resets_errors(self) -> None:
        rotator = GeminiAPIRotator(["key-a"], requests_per_minute=60)
        rotator.record_error(0, "timeout")
        # After success the consecutive_errors should reset
        rotator.record_success(0)
        state = rotator._states[0]
        assert state.consecutive_errors == 0

    def test_record_error_sets_backoff(self) -> None:
        rotator = GeminiAPIRotator(["key-a"], requests_per_minute=60)
        before = time.monotonic()
        rotator.record_error(0, "rate_limit")
        state = rotator._states[0]
        # First error → 60 s backoff
        assert state.backoff_until > before + 59

    def test_exponential_backoff_caps_at_600(self) -> None:
        rotator = GeminiAPIRotator(["key-a"], requests_per_minute=60)
        for _ in range(20):
            rotator.record_error(0)
        state = rotator._states[0]
        # backoff_until should be at most ~600 s from now
        max_expected = time.monotonic() + 601
        assert state.backoff_until <= max_expected

    def test_rate_limit_exhausts_single_key(self) -> None:
        """After RPM requests in the window, the key should no longer be available."""
        rotator = GeminiAPIRotator(["only-key"], requests_per_minute=3)
        for _ in range(3):
            rotator.get_next_available_key()
        # 4th call on a single key would block; just check _is_available directly
        state = rotator._states[0]
        assert not rotator._is_available(state)

    def test_seconds_until_available_no_backoff(self) -> None:
        rotator = GeminiAPIRotator(["key-a", "key-b"], requests_per_minute=60)
        secs = rotator.seconds_until_available()
        assert secs == 0.0

    def test_is_available_after_window_expires(self) -> None:
        """Fake-age the timestamps so they fall outside the 60-s window."""
        rotator = GeminiAPIRotator(["key-a"], requests_per_minute=2)
        state = rotator._states[0]
        # Manually push two old timestamps (> 60 s ago)
        old_time = time.monotonic() - 65
        state.request_times.append(old_time)
        state.request_times.append(old_time)
        # Now _is_available should purge them and return True
        assert rotator._is_available(state)
