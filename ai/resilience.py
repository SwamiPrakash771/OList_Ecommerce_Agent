"""LLM provider failures: what kind they are, when to stop calling, and what to tell the customer.

Seen in this project: Gemini's 15-requests-per-minute limit and its daily quota ("retry in
83,760 s"), both of which used to show the same generic "ran into a problem" on every message.

- classify(exc): rate_limited | quota | timeout | provider_error | bad_request | auth | unknown
- a circuit breaker: after repeated failures the provider is skipped for a while, so customers get
  an immediate answer instead of each waiting for the same failure. Simple questions are still
  answered locally when the role is in hybrid mode (ai/fast_path.py), so the assistant degrades
  rather than going silent.
- message(category): what the customer sees for each case.

State is per server process (in memory) and is reported on the admin AI settings page and by
/api/ready.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections import deque

from pydantic_ai.exceptions import FallbackExceptionGroup, ModelAPIError, ModelHTTPError

FAILURE_WINDOW_S = 120     # failures counted within this window
FAILURES_TO_OPEN = 3       # ...this many open the circuit
OPEN_FOR_S = 120           # provider errors / timeouts: skip the provider for this long
MAX_OPEN_S = 1800          # re-check at least every 30 minutes (e.g. after billing is enabled)

MESSAGES = {
    "rate_limited": "I'm getting a lot of questions right now. Please try again in a minute.",
    "quota": "The assistant has reached its usage limit for now and will be back later.",
    "timeout": "That took too long to answer. Please try again, or ask a simpler question.",
    "provider_error": "The assistant is having trouble right now. Please try again in a few minutes.",
    "auth": "The assistant isn't available right now. Our team has been notified.",
    "bad_request": "Sorry, I couldn't process that request. Please try rephrasing it.",
    "unknown": "The assistant ran into a problem. Please try again.",
}
LOCAL_STILL_WORKS = (" Simple questions, like where your order or refund is, or what's in your cart, "
                     "still work.")

# How bad each kind is when several fallbacks failed differently (the worst one decides).
_SEVERITY = ["unknown", "bad_request", "timeout", "rate_limited", "provider_error", "quota", "auth"]

_lock = threading.Lock()
_failures: deque[float] = deque()
_open_until = 0.0
_open_reason: str | None = None
_last_error: dict | None = None


def _text(body) -> str:
    return str(body or "")


def retry_after_s(exc: BaseException) -> float | None:
    """The provider's suggested wait, if it gave one (Gemini: retryDelay '37s'; or a header)."""
    if isinstance(exc, ModelHTTPError):
        headers = getattr(exc, "headers", None) or {}
        if str(headers.get("retry-after", "")).isdigit():
            return float(headers["retry-after"])
        m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", _text(exc.body))
        if m:
            return float(m.group(1))
    return None


def classify(exc: BaseException) -> str:
    if isinstance(exc, FallbackExceptionGroup):   # every model failed: the worst kind decides
        kinds = [classify(e) for e in exc.exceptions] or ["unknown"]
        return max(kinds, key=_SEVERITY.index)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, ModelHTTPError):
        code, body = exc.status_code, _text(exc.body)
        if code == 429:
            daily = re.search(r"PerDay|per day|daily", body, re.I)
            long_wait = (retry_after_s(exc) or 0) > 300
            return "quota" if daily or long_wait else "rate_limited"
        if code in (401, 403):
            return "auth"
        if code in (408, 504):
            return "timeout"
        if code >= 500:
            return "provider_error"
        return "bad_request"
    if isinstance(exc, ModelAPIError):
        return "timeout" if re.search(r"time.?out", str(exc), re.I) else "provider_error"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connect" in name or "network" in name:
        return "provider_error"
    return "unknown"


def retryable(category: str) -> bool:
    """Worth one quick retry before answering: short-lived limits only."""
    return category == "rate_limited"


def message(category: str, *, hybrid: bool = False) -> str:
    text = MESSAGES.get(category, MESSAGES["unknown"])
    if hybrid and category in ("quota", "provider_error", "rate_limited", "auth"):
        text += LOCAL_STILL_WORKS
    return text


# ---- circuit breaker ------------------------------------------------------------------------


def record_failure(category: str, exc: BaseException | None = None) -> None:
    global _open_until, _open_reason, _last_error
    now = time.time()
    with _lock:
        _last_error = {"category": category, "at": now, "detail": str(exc)[:200] if exc else None}
        if category in ("bad_request", "unknown"):
            return   # our side or one-off: not a provider outage
        if category in ("quota", "auth"):   # won't fix itself in seconds: stop calling now
            wait = retry_after_s(exc) if exc else None
            _open_until, _open_reason = now + min(wait or 600, MAX_OPEN_S), category
            return
        _failures.append(now)
        while _failures and _failures[0] < now - FAILURE_WINDOW_S:
            _failures.popleft()
        if len(_failures) >= FAILURES_TO_OPEN:
            wait = (retry_after_s(exc) if exc else None) if category == "rate_limited" else None
            _open_until, _open_reason = now + min(wait or OPEN_FOR_S, MAX_OPEN_S), category
            _failures.clear()


def record_success() -> None:
    global _open_until, _open_reason
    with _lock:
        _failures.clear()
        _open_until, _open_reason = 0.0, None


def is_open() -> tuple[bool, str | None]:
    """(skip the provider?, why). After the pause, one request is let through to re-check."""
    with _lock:
        if _open_until and time.time() < _open_until:
            return True, _open_reason
        return False, None


def state() -> dict:
    """For /api/ready and the admin page."""
    with _lock:
        now = time.time()
        open_ = bool(_open_until and now < _open_until)
        return {
            "status": f"paused ({_open_reason})" if open_ else ("degraded" if _failures else "ok"),
            "paused_for_s": round(_open_until - now) if open_ else 0,
            "recent_failures": len(_failures),
            "last_error": ({**_last_error, "seconds_ago": round(now - _last_error["at"])}
                           if _last_error else None),
        }


def reset() -> None:
    """Admin action (e.g. after enabling billing): try the provider again right away."""
    record_success()
