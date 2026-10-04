"""Polite HTTP client: rate limiting, 429 backoff, timeouts and bad payloads.

The client is the only thing standing between ScoutForge and Roblox's public
APIs, so these tests cover exactly the failure modes that actually happen:
throttling, timeouts, 5xx, HTML error pages and truncated JSON.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from scoutforge.net.client import (
    MalformedResponseError,
    PoliteHttpClient,
    TransportError,
)

BASE = "https://games.example.test"
URL = f"{BASE}/v1/games"


@pytest.fixture
async def client() -> PoliteHttpClient:
    http = PoliteHttpClient(
        rate_limit_rps=1000.0,  # effectively unlimited: this is not a rate-limit test
        timeout_seconds=1.0,
        max_retries=2,
        backoff_base_seconds=0.001,
    )
    try:
        yield http
    finally:
        await http.aclose()


def _response(status: int, **kwargs) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", URL), **kwargs)


class TestSuccess:
    @respx.mock
    async def test_decodes_json(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(200, json={"data": [1, 2, 3]}))
        assert await client.get_json(URL) == {"data": [1, 2, 3]}

    @respx.mock
    async def test_empty_body_is_an_empty_dict(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(200, content=b""))
        assert await client.get_json(URL) == {}

    @respx.mock
    async def test_query_params_are_sent(self, client: PoliteHttpClient) -> None:
        route = respx.get(URL, params={"limit": "10"}).mock(return_value=_response(200, json=[]))
        assert await client.get_json(URL, params={"limit": 10}) == []
        assert route.called

    @respx.mock
    async def test_stats_are_recorded(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(200, json={}))
        await client.get_json(URL)
        assert client.stats.requests == 1
        assert client.stats.errors == 0
        assert client.stats.by_status[200] == 1


class TestMalformed:
    @respx.mock
    async def test_html_error_page_raises_malformed(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(
            return_value=_response(200, content=b"<html>maintenance</html>", text="x")
        )
        with pytest.raises(MalformedResponseError):
            await client.get_json(URL)
        assert client.stats.malformed == 1

    @respx.mock
    async def test_truncated_json_raises_malformed(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(200, content=b'{"data": [1, 2'))
        with pytest.raises(MalformedResponseError):
            await client.get_json(URL)

    @respx.mock
    async def test_malformed_error_carries_context(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(200, content=b"not json"))
        with pytest.raises(MalformedResponseError) as excinfo:
            await client.get_json(URL)
        assert URL in str(excinfo.value)
        assert excinfo.value.status_code == 200

    @respx.mock
    async def test_malformed_is_not_retried(self, client: PoliteHttpClient) -> None:
        """A bad body will still be a bad body on retry; fail fast."""
        route = respx.get(URL).mock(return_value=_response(200, content=b"nope"))
        with pytest.raises(MalformedResponseError):
            await client.get_json(URL)
        assert route.call_count == 1


class TestTimeouts:
    @respx.mock
    async def test_timeout_is_retried_then_raised(self, client: PoliteHttpClient) -> None:
        route = respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(TransportError):
            await client.get_json(URL)
        assert route.call_count == 3, "initial attempt plus two retries"
        assert client.stats.timeouts == 3

    @respx.mock
    async def test_timeout_then_success(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(
            side_effect=[httpx.ReadTimeout("slow"), _response(200, json={"ok": True})]
        )
        assert await client.get_json(URL) == {"ok": True}

    @respx.mock
    async def test_connection_error_is_retried(self, client: PoliteHttpClient) -> None:
        route = respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(TransportError):
            await client.get_json(URL)
        assert route.call_count == 3


class TestRateLimiting:
    @respx.mock
    async def test_429_is_retried_with_backoff(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(
            side_effect=[
                _response(429, headers={"Retry-After": "1"}),
                _response(200, json={"ok": True}),
            ]
        )
        assert await client.get_json(URL) == {"ok": True}
        assert client.stats.rate_limited == 1

    @respx.mock
    async def test_429_exhausting_retries_raises(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(429, headers={"Retry-After": "1"}))
        with pytest.raises(TransportError) as excinfo:
            await client.get_json(URL)
        assert excinfo.value.status_code == 429
        assert client.stats.rate_limited == 3

    def test_retry_after_seconds_parsing(self) -> None:
        assert (
            PoliteHttpClient._retry_after_seconds(_response(429, headers={"Retry-After": "7"}))
            == 7.0
        )

    def test_retry_after_defaults_to_one_second(self) -> None:
        assert PoliteHttpClient._retry_after_seconds(_response(429)) == 1.0

    def test_retry_after_is_clamped(self) -> None:
        """Never wait an hour because a header said so."""
        assert (
            PoliteHttpClient._retry_after_seconds(_response(429, headers={"Retry-After": "99999"}))
            == 60.0
        )

    def test_roblox_specific_header_is_honoured(self) -> None:
        response = _response(429, headers={"X-Roblox-TryAgain": "3"})
        assert PoliteHttpClient._retry_after_seconds(response) == 3.0

    def test_garbage_header_falls_back(self) -> None:
        response = _response(429, headers={"Retry-After": "soon-ish"})
        assert PoliteHttpClient._retry_after_seconds(response) == 1.0


class TestServerErrors:
    @respx.mock
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_retryable_statuses_are_retried(
        self, client: PoliteHttpClient, status: int
    ) -> None:
        route = respx.get(URL).mock(return_value=_response(status))
        with pytest.raises(TransportError):
            await client.get_json(URL)
        assert route.call_count == 3

    @respx.mock
    async def test_retryable_status_recovers(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(side_effect=[_response(503), _response(200, json={"ok": 1})])
        assert await client.get_json(URL) == {"ok": 1}

    @respx.mock
    async def test_404_is_not_retried(self, client: PoliteHttpClient) -> None:
        """A missing resource is an answer, not a transient fault."""
        route = respx.get(URL).mock(return_value=_response(404))
        with pytest.raises(TransportError) as excinfo:
            await client.get_json(URL)
        assert route.call_count == 1
        assert excinfo.value.status_code == 404

    @respx.mock
    async def test_400_is_not_retried(self, client: PoliteHttpClient) -> None:
        route = respx.get(URL).mock(return_value=_response(400))
        with pytest.raises(TransportError):
            await client.get_json(URL)
        assert route.call_count == 1


class TestExpectedStatus:
    @respx.mock
    async def test_expected_status_is_returned(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(404, json={"message": "gone"}))
        assert await client.get_json(URL, expected_status=(200, 404)) == {"message": "gone"}

    @respx.mock
    async def test_expected_status_still_decodes_json(self, client: PoliteHttpClient) -> None:
        respx.get(URL).mock(return_value=_response(404, content=b"<html>"))
        with pytest.raises(MalformedResponseError):
            await client.get_json(URL, expected_status=(404,))


class TestBackoff:
    def test_backoff_is_bounded_and_non_negative(self, client: PoliteHttpClient) -> None:
        assert all(0.0 <= client._retry_delay(a) <= 31.0 for a in range(10))

    def test_backoff_trend_grows_despite_jitter(self) -> None:
        """Full jitter scatters individual samples, so compare distributions."""
        import statistics

        http = PoliteHttpClient(backoff_base_seconds=1.0)
        early = [http._retry_delay(0) for _ in range(50)]
        late = [http._retry_delay(5) for _ in range(50)]
        assert statistics.mean(late) > statistics.mean(early)

    def test_backoff_is_capped(self, client: PoliteHttpClient) -> None:
        assert client._retry_delay(50) <= 31.0


class TestContextManager:
    @respx.mock
    async def test_aclose_is_idempotent(self) -> None:
        async with PoliteHttpClient(rate_limit_rps=1000.0) as http:
            respx.get(URL).mock(return_value=_response(200, json={}))
            await http.get_json(URL)
        await http.aclose()
