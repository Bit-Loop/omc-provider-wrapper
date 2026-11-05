# =========================================
# File: agent_chat_api/rate_limit.py
# Purpose:
#   - Provides lightweight in-memory request throttling for the chat API.
# What This File Does:
#   - Tracks request counts per fixed time window using either the authorization
#     header digest or the client IP address as the bucket key.
#   - Returns both allow/deny decisions and HTTP-style `X-RateLimit-*` response
#     headers.
#   - Honors settings that disable rate limiting or change the window/request
#     limits.
# Main Classes:
#   - `RateLimitDecision`: Immutable result describing whether a request is
#     allowed, how many remain, and when the bucket resets.
#   - `InMemoryRateLimiter`: Stores per-bucket counts in memory and evaluates
#     the current request against the configured window.
# Main Methods:
#   - `RateLimitDecision.headers()`: Formats the decision into HTTP response
#     headers.
#   - `InMemoryRateLimiter.check()`: Advances/reset buckets by window and
#     returns the allow/deny decision.
# Main Functions:
#   - `bucket_key_for_request()`: Hashes authorization values so tokens are not
#     stored raw, and falls back to the client IP when no auth header exists.
# Used By:
#   - `agent_chat_api/tests/test_sessions_rate.py` imports `InMemoryRateLimiter`, `bucket_key_for_request` from this file.
# Reader Note:
#   - This limiter is process-local. It is suitable for this standalone wrapper,
#     but it is not a distributed quota store.
# - 2026-06-06 00:30:48 -0500
# =========================================
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from threading import Lock

from .config import Settings


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_epoch: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset_epoch),
        }


class InMemoryRateLimiter:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.rate_limit_enabled
        self.window_seconds = settings.rate_limit_window_seconds
        self.max_requests = settings.rate_limit_requests
        self._buckets: dict[str, tuple[int, int]] = {}
        self._lock = Lock()

    def check(self, bucket_key: str, now: float | None = None) -> RateLimitDecision:
        current = time.time() if now is None else now
        if not self.enabled:
            return RateLimitDecision(True, self.max_requests, self.max_requests, int(current))

        window = int(current // self.window_seconds)
        reset_epoch = int((window + 1) * self.window_seconds)
        with self._lock:
            stored_window, count = self._buckets.get(bucket_key, (window, 0))
            if stored_window != window:
                count = 0
                stored_window = window
            if count >= self.max_requests:
                self._buckets[bucket_key] = (stored_window, count)
                return RateLimitDecision(False, self.max_requests, 0, reset_epoch)
            count += 1
            self._buckets[bucket_key] = (stored_window, count)
            return RateLimitDecision(True, self.max_requests, self.max_requests - count, reset_epoch)


def bucket_key_for_request(*, authorization: str | None, client_ip: str) -> str:
    if authorization:
        digest = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
        return f"auth:{digest}"
    return f"ip:{client_ip}"
