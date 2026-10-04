"""Discovery source registry: unknown names degrade, they never crash."""

from __future__ import annotations

import logging

import pytest

from scoutforge.config.settings import DiscoverySettings, RobloxApiSettings
from scoutforge.discovery.registry import available_sources, build_sources
from scoutforge.net.client import PoliteHttpClient


@pytest.fixture
def http() -> PoliteHttpClient:
    return PoliteHttpClient()


class TestRegisteredNames:
    def test_the_three_roblox_sources_are_registered(self) -> None:
        assert available_sources() == ["roblox_explore", "roblox_search", "roblox_similar"]


class TestBuildSources:
    async def test_known_names_build(self, http: PoliteHttpClient) -> None:
        bundle = build_sources(
            http,
            discovery=DiscoverySettings(
                enabled_sources=["roblox_explore", "roblox_search", "roblox_similar"]
            ),
            roblox=RobloxApiSettings(),
        )

        assert bundle.names() == ["roblox_explore", "roblox_search", "roblox_similar"]

    async def test_unknown_name_is_skipped_and_logged(
        self, http: PoliteHttpClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The regression: this used to raise KeyError('Attempt to overwrite name')."""
        with caplog.at_level(logging.WARNING, logger="scoutforge.discovery.registry"):
            bundle = build_sources(
                http,
                discovery=DiscoverySettings(enabled_sources=["roblox_typo", "roblox_explore"]),
                roblox=RobloxApiSettings(),
            )

        assert bundle.names() == ["roblox_explore"]
        warnings = [r for r in caplog.records if r.getMessage() == "[SOURCE_UNKNOWN]"]
        assert len(warnings) == 1
        assert warnings[0].source_name == "roblox_typo"
        assert warnings[0].available == available_sources()

    async def test_json_style_env_value_still_resolves(self, http: PoliteHttpClient) -> None:
        """ENABLED_SOURCES='["roblox_explore","roblox_search"]' must not resolve to nothing."""
        bundle = build_sources(
            http,
            discovery=DiscoverySettings(enabled_sources='["roblox_explore","roblox_search"]'),
            roblox=RobloxApiSettings(),
        )

        assert bundle.names() == ["roblox_explore", "roblox_search"]

    async def test_whitespace_is_tolerated(self, http: PoliteHttpClient) -> None:
        bundle = build_sources(
            http,
            discovery=DiscoverySettings(enabled_sources=" roblox_explore , roblox_search "),
            roblox=RobloxApiSettings(),
        )

        assert bundle.names() == ["roblox_explore", "roblox_search"]
