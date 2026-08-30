"""Retry classification shared by the Gemini call sites.

Split out of ai.cover_letter so the on-demand recruiter message can retry on
the same terms as the background cover letter worker. The distinction that
matters is not the exception type the SDK raises but whether waiting will
help: a quota bounce clears on its own, a 500 usually does, a malformed
request never will.
"""
from __future__ import annotations


class RateLimitError(Exception):
    """Quota or rate limit. Retryable, and the key should be rotated."""


class TemporaryError(Exception):
    """Provider-side hiccup or timeout. Retryable as-is."""


def classify_exception(exc: Exception) -> Exception:
    """Re-raise an API exception as retryable or fatal.

    Matches on the message rather than the type because google-genai wraps
    most failures in the same class and the status code is what separates
    "try again" from "this request is wrong".
    """
    msg = str(exc).lower()
    if "quota" in msg or "rate" in msg or "429" in msg:
        return RateLimitError(str(exc))
    if "503" in msg or "500" in msg or "timeout" in msg:
        return TemporaryError(str(exc))
    return exc
