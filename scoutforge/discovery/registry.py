"""Registry mapping source names to constructors.

To add a data source:

1. implement :class:`~scoutforge.discovery.base.GameDiscoverySource`
2. register a factory here under a unique ``name``
3. add the name to ``ENABLED_SOURCES``

No other module needs to change - the worker only knows names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import DiscoverySettings, RobloxApiSettings
from scoutforge.discovery.base import EnrichmentCapableSource, GameDiscoverySource
from scoutforge.discovery.sources.roblox_api import RobloxApiClient
from scoutforge.discovery.sources.roblox_explore import RobloxExploreSource
from scoutforge.discovery.sources.roblox_search import RobloxSearchSource
from scoutforge.discovery.sources.roblox_similar import RobloxSimilarSource
from scoutforge.net.client import PoliteHttpClient

log = get_logger(__name__)

SourceFactory = Callable[[RobloxApiClient, DiscoverySettings], GameDiscoverySource]

_REGISTRY: dict[str, SourceFactory] = {}


def register(name: str, factory: SourceFactory) -> None:
    """Register (or replace) a discovery source factory."""
    _REGISTRY[name] = factory


def available_sources() -> list[str]:
    """Names of every registered discovery source."""
    return sorted(_REGISTRY)


def _factory_explore(api: RobloxApiClient, cfg: DiscoverySettings) -> GameDiscoverySource:
    return RobloxExploreSource(api, pages_per_scan=cfg.explore_pages)


def _factory_search(api: RobloxApiClient, cfg: DiscoverySettings) -> GameDiscoverySource:
    return RobloxSearchSource(
        api,
        terms=tuple(cfg.search_terms),
        terms_per_scan=cfg.search_terms_per_scan,
    )


def _factory_similar(api: RobloxApiClient, cfg: DiscoverySettings) -> GameDiscoverySource:
    return RobloxSimilarSource(api)


register(RobloxExploreSource.name, _factory_explore)
register(RobloxSearchSource.name, _factory_search)
register(RobloxSimilarSource.name, _factory_similar)


@dataclass(slots=True)
class SourceBundle:
    """Instantiated sources plus the shared API client."""

    sources: list[GameDiscoverySource]
    api: RobloxApiClient

    def enrichment_sources(self) -> list[EnrichmentCapableSource]:
        return [s for s in self.sources if isinstance(s, EnrichmentCapableSource)]

    def names(self) -> list[str]:
        return [s.name for s in self.sources]

    async def aclose(self) -> None:
        for source in self.sources:
            await source.aclose()


def build_sources(
    http: PoliteHttpClient,
    *,
    discovery: DiscoverySettings | None = None,
    roblox: RobloxApiSettings | None = None,
) -> SourceBundle:
    """Instantiate the configured discovery sources.

    Unknown names are logged and skipped instead of crashing the worker - a typo
    in ``ENABLED_SOURCES`` degrades gracefully.
    """
    from scoutforge.config.settings import get_settings

    discovery = discovery or get_settings().discovery
    roblox = roblox or get_settings().roblox

    api = RobloxApiClient(http, api_base=roblox.api_base, games_base=roblox.api_base)

    sources: list[GameDiscoverySource] = []
    for raw_name in discovery.enabled_sources:
        name = raw_name.strip()
        factory = _REGISTRY.get(name)
        if factory is None:
            log_event(log, 30, "SOURCE_UNKNOWN", name=name, available=available_sources())
            continue
        sources.append(factory(api, discovery))

    log_event(log, 20, "SOURCES_READY", count=len(sources), names=[s.name for s in sources])
    return SourceBundle(sources=sources, api=api)


def register_source(name: str, factory: SourceFactory) -> None:
    """Public alias of :func:`register` for third-party plugins."""
    register(name, factory)


def iter_registered() -> Iterable[tuple[str, SourceFactory]]:
    return _REGISTRY.items()


__all__ = [
    "SourceBundle",
    "available_sources",
    "build_sources",
    "iter_registered",
    "register",
    "register_source",
]
