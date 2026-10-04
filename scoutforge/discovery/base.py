"""The pluggable discovery interface.

Every data source implements :class:`GameDiscoverySource`. Adding a new source
means writing one module and registering it in
:mod:`scoutforge.discovery.registry` - nothing else in the codebase changes.
"""

from __future__ import annotations

import abc
from typing import Any

from scoutforge.discovery.models import (
    CreatorInfo,
    DiscoveryBatch,
    GameCandidate,
    GameDetails,
)


class GameDiscoverySource(abc.ABC):
    """A source of Roblox game candidates.

    Implementations must be:

    * **async** - all network work uses ``await``
    * **polite** - they should route calls through
      :class:`scoutforge.net.client.PoliteHttpClient`
    * **fault tolerant** - upstream errors are raised as
      :class:`scoutforge.net.client.TransportError` or returned inside
      :class:`~scoutforge.discovery.models.DiscoveryBatch`, never as unhandled
      crashes
    """

    #: Stable identifier used in configuration (``ENABLED_SOURCES``) and logs.
    name: str = "abstract"

    #: Human readable description shown in ``/stats`` and the dashboard.
    description: str = ""

    @abc.abstractmethod
    async def discover_games(self, limit: int = 100) -> DiscoveryBatch:
        """Return up to ``limit`` candidate games.

        Candidates are *cheap*: only identifiers and coarse hints are needed.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_game_details(self, universe_id: int) -> GameDetails | None:
        """Return the full metric payload for ``universe_id``.

        Returns ``None`` when the universe does not exist or is not accessible.
        """
        raise NotImplementedError

    async def get_creator_details(self, creator_id: int) -> CreatorInfo | None:
        """Return details about a user or group. Optional - ``None`` if unsupported."""
        return None

    async def get_social_links(self, universe_id: int) -> dict[str, str]:
        """Return *publicly advertised* links for a game.

        Only links the creator has deliberately published on the game's Roblox
        page are returned (e.g. a Discord server listed in the description).
        Empty dict when nothing is legitimately available.
        """
        return {}

    async def aclose(self) -> None:
        """Release any resources held by the source."""
        return None

    def health(self) -> dict[str, Any]:
        """Return a small dict describing the source for observability."""
        return {"name": self.name, "description": self.description, "type": type(self).__name__}

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<{type(self).__name__} name={self.name!r}>"


class EnrichmentCapableSource(GameDiscoverySource):
    """Convenience base for sources that can return candidates *with* metrics.

    Some endpoints (Roblox Explore, for instance) already include CCU and visit
    counts. When a source implements :meth:`discover_with_details`, the worker
    can skip per-universe enrichment and save a large number of API calls.
    """

    async def discover_with_details(self, limit: int = 100) -> list[GameDetails]:
        """Return enriched details straight from discovery. Default: empty list."""
        return []


class SeededDiscoverySource(GameDiscoverySource):
    """A source that expands from inputs supplied by the worker.

    Recommendation-style feeds need a starting point (the games ScoutForge
    already tracks). The worker calls :meth:`set_seeds` before every scan.
    """

    def set_seeds(self, universe_ids: list[int]) -> None:
        """Provide the universes this source should expand from."""
        return None

    @staticmethod
    def _filter_seeds(universe_ids: list[int], *, min_universe_id: int | None = None) -> list[int]:
        unique = {uid for uid in universe_ids if uid > 0}
        if min_universe_id:
            unique = {uid for uid in unique if uid >= min_universe_id}
        return sorted(unique)


def candidate_from_details(details: GameDetails, source: str) -> GameCandidate:
    return GameCandidate(
        universe_id=details.universe_id,
        source=source,
        place_id=details.place_id or details.root_place_id,
        name=details.name,
        creator_id=details.creator.creator_id,
        creator_name=details.creator.creator_name,
        current_ccu=details.current_ccu,
        lifetime_visits=details.lifetime_visits,
        favorites=details.favorites,
        thumbnail_url=details.thumbnail_url,
    )


__all__ = [
    "EnrichmentCapableSource",
    "GameDiscoverySource",
    "SeededDiscoverySource",
    "candidate_from_details",
]
