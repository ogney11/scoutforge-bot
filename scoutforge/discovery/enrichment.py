"""Enrichment layer.

Discovery endpoints return thin payloads. :class:`GameEnricher` is the single
place responsible for turning a universe id into a full
:class:`~scoutforge.discovery.models.GameDetails`, batching calls so a 400-game
scan costs ~12 HTTP requests instead of 400.

An optional concurrency limiter prevents the enricher from out-running the
client's rate limiter and keeps memory flat on very large scans.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from scoutforge.config.logging import get_logger, log_event
from scoutforge.discovery.models import GameCandidate, GameDetails
from scoutforge.discovery.sources.roblox_api import RobloxApiClient
from scoutforge.net.client import PoliteHttpClient, TransportError

log = get_logger(__name__)


class GameEnricher:
    """Batch game detail fetcher built on the public Roblox endpoints."""

    def __init__(
        self,
        api: RobloxApiClient,
        *,
        http: PoliteHttpClient | None = None,
        concurrency: int = 4,
        resolve_creators: bool = False,
    ) -> None:
        self.api = api
        self.http = http
        self.concurrency = max(1, concurrency)
        self.resolve_creators = resolve_creators
        self.errors = 0

    async def enrich(self, universe_ids: Sequence[int]) -> dict[int, GameDetails]:
        """Fetch details for ``universe_ids`` (batched, 100 ids per request)."""
        unique: list[int] = []
        seen: set[int] = set()
        for universe_id in universe_ids:
            if universe_id > 0 and universe_id not in seen:
                seen.add(universe_id)
                unique.append(universe_id)

        if not unique:
            return {}

        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(ids: list[int]) -> dict[int, GameDetails]:
            async with semaphore:
                try:
                    return await self.api.get_details_batch(ids)
                except TransportError as exc:
                    self.errors += 1
                    log_event(log, 30, "ENRICH_FAILED", count=len(ids), error=str(exc)[:200])
                    return {}
                except Exception as exc:
                    self.errors += 1
                    log_event(
                        log,
                        30,
                        "ENRICH_ERROR",
                        count=len(ids),
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )
                    return {}

        chunks = _chunk(unique, 100)
        results = await asyncio.gather(*(fetch(chunk) for chunk in chunks))
        merged: dict[int, GameDetails] = {}
        for part in results:
            merged.update(part)

        if self.resolve_creators:
            await self._resolve_creators(merged)

        log_event(
            log,
            20,
            "ENRICHED",
            requested=len(unique),
            enriched=len(merged),
            requests=len(chunks),
            errors=self.errors,
        )
        return merged

    async def _resolve_creators(self, details: dict[int, GameDetails]) -> None:
        """Fill in creator type/group info for a bounded number of games."""
        creator_ids = {
            d.creator.creator_id
            for d in details.values()
            if d.creator.creator_id and not d.creator.creator_type
        }
        if not creator_ids:
            return
        semaphore = asyncio.Semaphore(self.concurrency)

        async def resolve(creator_id: int) -> None:
            async with semaphore:
                info = await self.api.resolve_creator(creator_id)
            if info is None:
                return
            for game_details in details.values():
                if game_details.creator.creator_id != creator_id:
                    continue
                game_details.creator.creator_type = info.creator_type
                game_details.creator.creator_url = info.creator_url
                if info.creator_type == "Group":
                    game_details.creator.group_id = info.group_id
                    game_details.creator.group_name = info.group_name
                if info.creator_name and not game_details.creator.creator_name:
                    game_details.creator.creator_name = info.creator_name

        await asyncio.gather(*(resolve(cid) for cid in list(creator_ids)[:100]))

    async def enrich_candidates(
        self, candidates: Sequence[GameCandidate]
    ) -> dict[int, GameDetails]:
        """Enrich a list of candidates and return details keyed by universe id."""
        details = await self.enrich([c.universe_id for c in candidates])
        if not self.resolve_creators:
            for candidate in candidates:
                game = details.get(candidate.universe_id)
                if game is None:
                    continue
                if game.creator.creator_name is None and candidate.creator_name:
                    game.creator.creator_name = candidate.creator_name
                if game.creator.creator_id is None and candidate.creator_id:
                    game.creator.creator_id = candidate.creator_id
                if game.name is None and candidate.name:
                    game.name = candidate.name
                if game.thumbnail_url is None and candidate.thumbnail_url:
                    game.thumbnail_url = candidate.thumbnail_url
                if game.current_ccu is None and candidate.current_ccu is not None:
                    game.current_ccu = candidate.current_ccu
                    if "current_ccu" in game.missing_metrics:
                        game.missing_metrics.remove("current_ccu")
                if game.lifetime_visits is None and candidate.lifetime_visits is not None:
                    game.lifetime_visits = candidate.lifetime_visits
                    if "lifetime_visits" in game.missing_metrics:
                        game.missing_metrics.remove("lifetime_visits")
        return details

    async def social_links(self, universe_id: int) -> dict[str, str]:
        """Public links a creator has advertised for a game.

        Only the Discord invite published in the game's own description is
        returned - nothing is scraped from user profiles or private data.
        """
        try:
            games = await self.api.games_v2([universe_id])
        except TransportError as exc:
            self.errors += 1
            log_event(log, 30, "SOCIAL_LOOKUP_FAILED", universe=universe_id, error=str(exc)[:160])
            return {}
        record = games.get(universe_id)
        if not record:
            return {}
        from scoutforge.discovery.sources.roblox_api import extract_discord_url

        discord_url = extract_discord_url(record.get("description"))
        return {"discord": discord_url} if discord_url else {}


def _chunk(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = ["GameEnricher"]
