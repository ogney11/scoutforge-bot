"""Discovery source backed by the public Roblox Explore (discover) feeds.

``roblox.com/discover`` is assembled from "content sets". ScoutForge reads the
same public JSON the website reads:

1. ``GET /explore-api/v1/get-home-page-details`` -> list of content sets
2. ``GET /explore-api/v1/get-sort-content?sortId=<setId>`` -> paged universe ids

This is the closest legitimate analogue to "what is popular on Roblox right
now", and it needs no credentials.
"""

from __future__ import annotations

from typing import Any

from scoutforge.config.logging import get_logger, log_event
from scoutforge.discovery.base import EnrichmentCapableSource
from scoutforge.discovery.models import (
    DiscoveryBatch,
    GameCandidate,
    GameDetails,
    coerce_int,
    coerce_str,
)
from scoutforge.discovery.sources.roblox_api import RobloxApiClient

log = get_logger(__name__)


class RobloxExploreSource(EnrichmentCapableSource):
    """Curated popularity feeds (most popular, trending, new releases...)."""

    name = "roblox_explore"
    description = "Roblox Explore/discover popularity feeds"

    def __init__(self, api: RobloxApiClient, *, pages_per_scan: int = 3) -> None:
        self.api = api
        self.pages_per_scan = max(1, pages_per_scan)
        self._sort_ids: list[str] = []

    async def refresh_sort_ids(self) -> list[str]:
        """Fetch (and cache) the content set ids currently on the home page."""
        if self._sort_ids:
            return self._sort_ids
        sections = await self.api.home_page_sections()
        ids: list[str] = []
        for section in sections:
            for entry in section.get("contents", []) or []:
                if not isinstance(entry, dict):
                    continue
                set_id = coerce_str(entry.get("setId"))
                if set_id and set_id not in ids:
                    ids.append(set_id)
            set_id = coerce_str(section.get("setId"))
            if set_id and set_id not in ids:
                ids.append(set_id)
        self._sort_ids = ids
        log_event(log, 20, "EXPLORE_SETS", count=len(ids))
        return ids

    async def discover_games(self, limit: int = 100) -> DiscoveryBatch:
        batch = DiscoveryBatch(source=self.name)
        sort_ids = await self.refresh_sort_ids()
        if not sort_ids:
            batch.notes = "no explore content sets returned"
            return batch

        api = self.api
        per_feed = max(1, min(self.pages_per_scan, max(1, limit // max(1, len(sort_ids)) or 1)))
        seen: set[int] = set()

        for sort_id in sort_ids:
            page_token: str | None = None
            for _page in range(per_feed):
                if len(seen) >= limit:
                    break
                try:
                    payload = await api.sort_content(sort_id, page_token=page_token)
                except Exception as exc:
                    batch.errors += 1
                    log_event(
                        log, 25, "EXPLORE_FEED_FAILED", set_id=sort_id, error=type(exc).__name__
                    )
                    break
                contents = payload.get("contents") if isinstance(payload, dict) else None
                if not isinstance(contents, list) or not contents:
                    break
                page_token = coerce_str(payload.get("nextPageToken"))
                for record in contents:
                    candidate = self._parse_content(record, len(seen), sort_id)
                    if candidate is None or candidate.universe_id in seen:
                        continue
                    seen.add(candidate.universe_id)
                    batch.candidates.append(candidate)
                    if len(seen) >= limit:
                        break
                batch.pages_fetched += 1
                if not page_token:
                    break

        log_event(
            log,
            20,
            "DISCOVERY",
            source=self.name,
            candidates=len(batch.candidates),
            pages=batch.pages_fetched,
            errors=batch.errors,
        )
        return batch

    @staticmethod
    def _parse_content(record: Any, rank: int, sort_id: str) -> GameCandidate | None:
        if not isinstance(record, dict):
            return None
        universe_id = coerce_int(record.get("universeId")) or coerce_int(record.get("universeID"))
        if universe_id is None:
            return None
        return GameCandidate(
            universe_id=universe_id,
            source=RobloxExploreSource.name,
            place_id=coerce_int(record.get("placeId")),
            name=coerce_str(record.get("name"), max_length=256),
            creator_name=coerce_str(record.get("creatorName"), max_length=256),
            current_ccu=coerce_int(record.get("playerCount")) or coerce_int(record.get("players")),
            lifetime_visits=coerce_int(record.get("totalUpVotes")),
            thumbnail_url=coerce_str(record.get("thumbnailUrl")),
            discovery_rank=rank,
            discovery_context={"sort_id": sort_id},
        )

    async def get_game_details(self, universe_id: int) -> GameDetails | None:
        """Full public metric payload for one universe.

        All three bundled sources read the same public endpoints, so they share
        this delegation; sources with their own endpoints override it.
        """
        return await self.api.get_details(universe_id)


__all__ = ["RobloxExploreSource"]
