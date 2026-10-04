"""Discovery source built on Roblox's public "more like this" recommendations.

The recommendation feed is the single most useful *emerging game* detector
available through public APIs: Roblox seeds it with games similar to the ones
people already play, so a brand new universe with 1k+ CCU shows up next to
established hits long before it surfaces on generic popularity feeds.

The source needs a starting point, so the worker calls :meth:`set_seeds` with
the universes ScoutForge already tracks. With no seeds it simply returns an
empty batch - which is why it is safe to enable by default.
"""

from __future__ import annotations

import random
from typing import Any

from scoutforge.config.logging import get_logger, log_event
from scoutforge.discovery.base import SeededDiscoverySource
from scoutforge.discovery.models import (
    DiscoveryBatch,
    GameCandidate,
    GameDetails,
    coerce_int,
    coerce_str,
)
from scoutforge.discovery.sources.roblox_api import RobloxApiClient

log = get_logger(__name__)


class RobloxSimilarSource(SeededDiscoverySource):
    """Universe recommendations derived from already-tracked games."""

    name = "roblox_similar"
    description = "Roblox recommendation feed seeded from tracked games"

    def __init__(
        self,
        api: RobloxApiClient,
        *,
        max_seeds: int = 12,
        min_universe_id: int | None = None,
    ) -> None:
        self.api = api
        self.max_seeds = max(1, max_seeds)
        self.min_universe_id = min_universe_id
        self._seeds: list[int] = []

    def set_seeds(self, universe_ids: list[int]) -> None:
        """Seed the source with universes to expand from."""
        unique = {uid for uid in universe_ids if uid > 0}
        if self.min_universe_id:
            unique = {uid for uid in unique if uid >= self.min_universe_id}
        self._seeds = sorted(unique)[: self.max_seeds]

    async def discover_games(self, limit: int = 100) -> DiscoveryBatch:
        batch = DiscoveryBatch(source=self.name)
        if not self._seeds:
            batch.notes = "no seeds configured; worker must call set_seeds() first"
            return batch

        seeds = list(self._seeds)
        random.shuffle(seeds)
        seen: set[int] = set()
        seed_set = set(seeds)

        for seed in seeds:
            if len(seen) >= limit:
                break
            for record in await self.api.recommendations_for_universe(seed):
                candidate = self._parse_content(record, len(seen), seed)
                if candidate is None:
                    continue
                if candidate.universe_id in seen or candidate.universe_id in seed_set:
                    continue
                seen.add(candidate.universe_id)
                batch.candidates.append(candidate)
                if len(seen) >= limit:
                    break

        batch.pages_fetched = len(seeds)
        log_event(
            log,
            20,
            "DISCOVERY",
            source=self.name,
            candidates=len(batch.candidates),
            seeds=len(seeds),
            errors=batch.errors,
        )
        return batch

    @staticmethod
    def _parse_content(record: Any, rank: int, seed: int) -> GameCandidate | None:
        if not isinstance(record, dict):
            return None
        universe_id = coerce_int(record.get("universeId")) or coerce_int(record.get("universeID"))
        if universe_id is None:
            return None
        return GameCandidate(
            universe_id=universe_id,
            source=RobloxSimilarSource.name,
            name=coerce_str(record.get("name"), max_length=256),
            current_ccu=coerce_int(record.get("playerCount")) or coerce_int(record.get("players")),
            lifetime_visits=coerce_int(record.get("visits")),
            thumbnail_url=coerce_str(record.get("thumbnailUrl")),
            discovery_rank=rank,
            discovery_context={"seed_universe_id": seed},
        )

    async def get_game_details(self, universe_id: int) -> GameDetails | None:
        """Full public metric payload for one universe.

        All three bundled sources read the same public endpoints, so they share
        this delegation; sources with their own endpoints override it.
        """
        return await self.api.get_details(universe_id)


__all__ = ["RobloxSimilarSource"]
