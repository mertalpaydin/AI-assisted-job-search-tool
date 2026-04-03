from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _KeyState:
    index: int
    request_times: deque = field(default_factory=lambda: deque(maxlen=60))
    consecutive_errors: int = 0
    backoff_until: float = 0.0


class GeminiAPIRotator:
    """
    Round-robin Gemini API key rotation with per-key rate limiting and
    exponential backoff on errors.

    Usage:
        rotator = GeminiAPIRotator(api_keys, requests_per_minute=15)
        idx, key = rotator.get_next_available_key()
        try:
            ...call API...
            rotator.record_success(idx)
        except RateLimitError:
            rotator.record_error(idx, "rate_limit")
    """

    def __init__(self, api_keys: list[str], requests_per_minute: int = 15) -> None:
        if not api_keys:
            raise ValueError("At least one Gemini API key is required")
        self._rpm = requests_per_minute
        self._window = 60.0  # seconds
        self._states = [_KeyState(index=i) for i in range(len(api_keys))]
        self._keys = list(api_keys)
        self._current = 0
        self._lock = threading.Lock()

    def _is_available(self, state: _KeyState) -> bool:
        now = time.monotonic()
        if now < state.backoff_until:
            return False
        # Purge timestamps outside the rolling window
        cutoff = now - self._window
        while state.request_times and state.request_times[0] < cutoff:
            state.request_times.popleft()
        return len(state.request_times) < self._rpm

    def get_next_available_key(self) -> tuple[int, str]:
        """
        Return (key_index, api_key) for the next available key.
        Blocks until a key becomes available.
        """
        while True:
            with self._lock:
                # Try each key starting from current position
                for _ in range(len(self._states)):
                    state = self._states[self._current]
                    self._current = (self._current + 1) % len(self._states)
                    if self._is_available(state):
                        state.request_times.append(time.monotonic())
                        return state.index, self._keys[state.index]

            # All keys exhausted — wait a second and retry
            time.sleep(1.0)

    def record_success(self, key_index: int) -> None:
        with self._lock:
            self._states[key_index].consecutive_errors = 0

    def record_error(self, key_index: int, error_type: str = "unknown") -> None:
        with self._lock:
            state = self._states[key_index]
            state.consecutive_errors += 1
            # Exponential backoff: 60s, 120s, 240s … capped at 600s
            backoff = min(60 * (2 ** (state.consecutive_errors - 1)), 600)
            state.backoff_until = time.monotonic() + backoff

    def seconds_until_available(self) -> float:
        """Estimated seconds until any key is usable (for logging)."""
        with self._lock:
            now = time.monotonic()
            return max(0.0, min(
                (s.backoff_until - now) for s in self._states
            ))
