"""Async rate limiting primitives.

Roblox enforces per-IP limits and expects well behaved clients. ScoutForge
therefore funnels *every* outbound request through a token bucket so the
discovery worker can never accidentally hammer the public APIs, even when
running with high concurrency.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class RateLimiter:
    """Synchronous (test-friendly) minimum-interval rate limiter.

    Args:
        rate_per_second: Allowed requests per second. Values <= 0 disable limiting.
        min_interval: Explicit minimum seconds between two calls.
    """

    __slots__ = ("_last_call", "_min_interval")

    def __init__(
        self, rate_per_second: float | None = None, min_interval: float | None = None
    ) -> None:
        if rate_per_second is None and min_interval is None:
            raise ValueError("Provide either rate_per_second or min_interval")
        if min_interval is not None:
            self._min_interval = float(min_interval)
        else:
            rate = float(rate_per_second or 0)
            self._min_interval = 1.0 / rate if rate > 0 else 0.0
        self._last_call: float | None = None

    @property
    def min_interval(self) -> float:
        return self._min_interval

    def acquire(self, *, now: Callable[[], float] = time.monotonic) -> float:
        """Reserve a slot. Returns the number of seconds the caller must wait."""
        if self._min_interval <= 0:
            return 0.0
        current = now()
        if self._last_call is None:
            self._last_call = current
            return 0.0
        earliest = self._last_call + self._min_interval
        self._last_call = max(current, earliest)
        return max(0.0, earliest - current)

    def reset(self) -> None:
        self._last_call = None


class AsyncTokenBucket:
    """Async token bucket supporting burst capacity.

    Args:
        rate: Sustained tokens (requests) per second.
        capacity: Maximum burst size. Defaults to ``ceil(rate)`` with a floor of 1.
    """

    __slots__ = ("_capacity", "_lock", "_rate", "_tokens", "_updated")

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._rate = float(rate)
        self._capacity = float(capacity if capacity is not None else max(1.0, rate))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self, amount: float = 1.0) -> float:
        """Consume ``amount`` tokens, waiting if necessary. Returns seconds waited."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                if elapsed > 0:
                    self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                    self._updated = now
                if self._tokens >= amount:
                    self._tokens -= amount
                    return waited
                deficit = amount - self._tokens
                sleep_for = deficit / self._rate
            await asyncio.sleep(sleep_for)
            waited += sleep_for

    def penalize(self, seconds: float) -> None:
        """Drain the bucket after a 429 so the next request waits ``seconds``."""
        if seconds <= 0:
            return
        self._tokens = 0.0
        self._updated = time.monotonic() + seconds

    async def __aenter__(self) -> AsyncTokenBucket:
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


__all__ = ["AsyncTokenBucket", "RateLimiter"]
