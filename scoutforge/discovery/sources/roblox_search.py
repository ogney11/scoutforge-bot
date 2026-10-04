"""Discovery source backed by the public Roblox omni-search endpoint.

Keyword search is how a *human* scout finds games ("new", "trending", "obby"),
so ScoutForge rotates a configurable keyword list to surface niches that the
popularity feeds never show - which is exactly where under-followed games with
high CCU-per-visit hide.
"""

from __future__ import annotations

import random
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

#: Keywords that reliably surface emerging games across Roblox niches.
DEFAULT_TERMS = (
    "trending",
    "new",
    "popular",
    "viral",
    "roleplay",
    "simulator",
    "obby",
    "update",
    "update new",
    "beta",
)


class RobloxSearchSource(EnrichmentCapableSource):
    """Keyword-driven discovery with rotating terms and pagination."""

    name = "roblox_search"
    description = "Roblox omni-search keyword discovery"

    def __init__(
        self,
        api: RobloxApiClient,
        *,
        terms: tuple[str, ...] = DEFAULT_TERMS,
        terms_per_scan: int = 4,
        pages_per_term: int = 2,
    ) -> None:
        self.api = api
        self.terms = tuple(terms) or DEFAULT_TERMS
        self.terms_per_scan = max(0, terms_per_scan)
        self.pages_per_term = max(1, pages_per_term)

    def _pick_terms(self) -> list[str]:
        """Rotate through the term list so consecutive scans explore new ground."""
        pool = list(self.terms)
        count = min(self.terms_per_scan or len(pool), len(pool))
        if count >= len(pool):
            random.shuffle(pool)
            return pool
        return random.sample(pool, count)

    async def discover_games(self, limit: int = 100) -> DiscoveryBatch:
        batch = DiscoveryBatch(source=self.name)
        seen: set[int] = set()
        per_term = max(1, limit // max(1, self.terms_per_scan or 1))

        for term in self._pick_terms():
            page_token: str | None = None
            for _page in range(self.pages_per_term):
                if len(seen) >= limit:
                    break
                session = self.api.new_session_id()
                try:
                    payload = await self.api.omni_search(
                        term, page_token=page_token, session_id=session
                    )
                except Exception as exc:
                    batch.errors += 1
                    log_event(log, 25, "SEARCH_TERM_FAILED", term=term, error=type(exc).__name__)
                    break

                results = payload.get("searchResults") if isinstance(payload, dict) else None
                contents: list[Any] = []
                if isinstance(results, list) and results:
                    first = results[0]
                    if isinstance(first, dict):
                        contents = first.get("contents") or []
                for record in contents:
                    candidate = self._parse_content(record, len(seen), term)
                    if candidate is None or candidate.universe_id in seen:
                        continue
                    seen.add(candidate.universe_id)
                    batch.candidates.append(candidate)
                    if len(seen) >= per_term:
                        break
                batch.pages_fetched += 1
                page_token = coerce_str(payload.get("nextPageToken"))
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
    def _parse_content(record: Any, rank: int, term: str) -> GameCandidate | None:
        if not isinstance(record, dict):
            return None
        universe_id = coerce_int(record.get("universeId")) or coerce_int(record.get("universeID"))
        if universe_id is None:
            return None
        creator_raw = record.get("creator")
        creator_name = None
        creator_id = None
        if isinstance(creator_raw, dict):
            creator_name = coerce_str(creator_raw.get("name"), max_length=256)
            creator_id = coerce_int(creator_raw.get("id")) or coerce_int(creator_raw.get("userId"))
        elif isinstance(creator_raw, str):
            creator_name = coerce_str(creator_raw, max_length=256)

        return GameCandidate(
            universe_id=universe_id,
            source=RobloxSearchSource.name,
            place_id=coerce_int(record.get("placeId")),
            name=coerce_str(record.get("name"), max_length=256),
            creator_id=creator_id,
            creator_name=creator_name,
            current_ccu=coerce_int(record.get("playerCount")),
            lifetime_visits=coerce_int(record.get("totalUpVotes")),
            thumbnail_url=coerce_str(record.get("thumbnailUrl")),
            discovery_rank=rank,
            discovery_context={"term": term},
        )

    async def get_game_details(self, universe_id: int) -> GameDetails | None:
        """Full public metric payload for one universe.

        All three bundled sources read the same public endpoints, so they share
        this delegation; sources with their own endpoints override it.
        """
        return await self.api.get_details(universe_id)


__all__ = ["DEFAULT_TERMS", "RobloxSearchSource"]
