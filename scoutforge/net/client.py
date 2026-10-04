"""A deliberately polite HTTP client for public APIs.

Features:
  * per-host token bucket rate limiting (never exceed configured RPS)
  * bounded retries with exponential backoff + full jitter
  * correct ``429`` handling (``Retry-After`` / ``X-Roblox-TryAgain`` /
    ``RateLimit-*`` headers) which also throttles the bucket for the whole host
  * explicit connect/read timeouts
  * structured logging of every failure class
  * JSON decode failures surfaced as a typed error, never as a crash

The client does **not** attempt to defeat rate limits, signatures, CAPTCHAs or
any other access control. When the API says "slow down", ScoutForge slows down.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx

from scoutforge.config.logging import get_logger, log_event
from scoutforge.net.rate_limiter import AsyncTokenBucket

log = get_logger(__name__)

T = TypeVar("T")

#: HTTP statuses that are worth retrying.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """Raised when a request could not be completed after all retries."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class MalformedResponseError(TransportError):
    """Raised when an upstream response is not valid JSON / has wrong shape."""


@dataclass(slots=True)
class RequestStats:
    """Cumulative counters, surfaced by ``/stats``."""

    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    errors: int = 0
    timeouts: int = 0
    malformed: int = 0
    bytes_received: int = 0
    total_latency: float = 0.0
    by_status: dict[int, int] = field(default_factory=dict)

    @property
    def average_latency(self) -> float:
        return self.total_latency / self.requests if self.requests else 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "malformed": self.malformed,
            "bytes_received": self.bytes_received,
            "average_latency": round(self.average_latency, 4),
            "by_status": dict(sorted(self.by_status.items())),
        }


class PoliteHttpClient:
    """Async HTTP client with rate limiting, retries and observability.

    Args:
        rate_limit_rps: Sustained requests per second across all hosts.
        timeout_seconds: Total per-attempt timeout.
        max_retries: Additional attempts after the first one.
        backoff_base_seconds: Base delay for exponential backoff.
        user_agent: ``User-Agent`` header sent with every request.
        client: Inject a pre-built ``httpx.AsyncClient`` (tests / custom pools).
    """

    def __init__(
        self,
        *,
        rate_limit_rps: float = 8.0,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 0.75,
        user_agent: str = "ScoutForge/1.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bucket = AsyncTokenBucket(rate_limit_rps)
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_base_seconds
        self._user_agent = user_agent
        self.stats = RequestStats()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0)),
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    # ------------------------------------------------------------------ life
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> PoliteHttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --------------------------------------------------------------- requests
    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> Any:
        """GET ``url`` and decode the JSON body.

        Raises:
            TransportError: Network failure, timeout, or an unexpected status.
            MalformedResponseError: Body is not valid JSON.
        """
        request_headers = dict(headers or {})
        request_headers.setdefault("User-Agent", self._user_agent)

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._bucket.acquire()
            started = asyncio.get_running_loop().time()
            try:
                response = await self._client.get(url, params=params, headers=request_headers)
            except httpx.TimeoutException as exc:
                self.stats.timeouts += 1
                self.stats.errors += 1
                last_error = exc
                log_event(
                    log,
                    30,
                    "HTTP_TIMEOUT",
                    url=url,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._retry_delay(attempt))
                continue
            except httpx.HTTPError as exc:
                self.stats.errors += 1
                last_error = exc
                log_event(
                    log,
                    30,
                    "HTTP_ERROR",
                    url=url,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._retry_delay(attempt))
                continue

            self.stats.requests += 1
            self.stats.total_latency += asyncio.get_running_loop().time() - started
            self.stats.bytes_received += len(response.content or b"")
            self.stats.by_status[response.status_code] = (
                self.stats.by_status.get(response.status_code, 0) + 1
            )

            if response.status_code == 429:
                self.stats.rate_limited += 1
                wait = self._retry_after_seconds(response)
                if attempt >= self._max_retries:
                    log_event(log, 30, "HTTP_RATE_LIMITED", url=url, attempts=attempt + 1)
                    raise TransportError(
                        f"Rate limited by upstream after {attempt + 1} attempts",
                        status_code=429,
                        url=url,
                    ) from None
                self._bucket.penalize(wait)
                log_event(
                    log,
                    30,
                    "HTTP_RATE_LIMITED",
                    url=url,
                    attempt=attempt + 1,
                    backoff=round(wait, 2),
                )
                await asyncio.sleep(wait + self._jitter())
                continue

            if (
                response.status_code in RETRYABLE_STATUS
                and response.status_code not in expected_status
            ):
                last_error = TransportError(
                    f"Upstream returned {response.status_code}",
                    status_code=response.status_code,
                    url=url,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._retry_delay(attempt))
                continue

            if response.status_code not in expected_status:
                self.stats.errors += 1
                raise TransportError(
                    f"Unexpected status {response.status_code} for {url}",
                    status_code=response.status_code,
                    url=url,
                )

            return self._decode(response, url)

        self.stats.errors += 1
        raise TransportError(f"Request to {url} failed after retries: {last_error}", url=url)

    def _decode(self, response: httpx.Response, url: str) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            self.stats.malformed += 1
            log_event(log, 30, "HTTP_MALFORMED", url=url, error=type(exc).__name__)
            raise MalformedResponseError(
                f"Malformed JSON response from {url}", status_code=response.status_code, url=url
            ) from exc

    # ---------------------------------------------------------------- helpers
    def _retry_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter."""
        return min(30.0, self._backoff * (2**attempt)) + self._jitter()

    @staticmethod
    def _jitter() -> float:
        # S311: backoff jitter wants spread, not cryptographic randomness.
        return random.uniform(0.0, 0.25)  # noqa: S311

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        """Extract a wait hint from the various headers Roblox/CDNs may send."""
        candidates = (
            response.headers.get("X-Roblox-TryAgain"),
            response.headers.get("Retry-After"),
            response.headers.get("RateLimit-Reset"),
        )
        for raw in candidates:
            if not raw:
                continue
            text = raw.strip()
            if text.isdigit():
                return min(60.0, max(0.5, float(text)))
            try:
                when = parsedate_to_datetime(text)
            except (TypeError, ValueError):
                continue
            if when is not None:
                import datetime as _dt

                delta = when.timestamp() - _dt.datetime.now(_dt.UTC).timestamp()
                return min(60.0, max(0.5, delta))
        return 1.0


__all__ = [
    "MalformedResponseError",
    "PoliteHttpClient",
    "RequestStats",
    "TransportError",
]
