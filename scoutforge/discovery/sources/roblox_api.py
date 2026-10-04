"""Low level client for Roblox's **public, unauthenticated** APIs.

Endpoints used (all publicly documented, no login, no cookies, no scraping):

===========================================================  ==================
``GET /explore-api/v1/get-home-page-details``                 Discover feed IDs
``GET /explore-api/v1/get-sort-content``                      Popularity feeds
``GET /search-api/omni-search``                               Keyword search
``GET /recommendation-api/legacy/recommendations/universes/``  Similar games
``GET /v2/games?universeIds=``                                Full game record
``GET /v1/games/multiplayer?universeIds=``                    Live CCU / visits
``GET /v1/games/icons`` (thumbnails host)                     Thumbnail URL
``GET /v1/users/{id}`` / ``GET /v2/groups/{id}``              Creator record
===========================================================  ==================

Requests go through :class:`~scoutforge.net.client.PoliteHttpClient` so they are
rate limited, retried with exponential backoff and fully logged. Batched endpoints
are used wherever the API allows it (up to 100 ids per call) to minimise load.
"""

from __future__ import annotations

import uuid
from typing import Any

from scoutforge.config.logging import get_logger, log_event
from scoutforge.discovery.models import (
    CreatorInfo,
    GameDetails,
    coerce_bool,
    coerce_int,
    coerce_str,
    coerce_timestamp,
)
from scoutforge.net.client import PoliteHttpClient, TransportError

log = get_logger(__name__)

#: Discord invite hosts we recognise inside a *public* game description.
_DISCORD_HOSTS = ("discord.gg", "discord.com/invite", "discordapp.com/invite")

#: Punctuation trimmed from the end of an extracted invite URL.
_URL_TRIM_CHARS = frozenset(".,);<>()[]\"'")

#: Maximum ids accepted by the batched Roblox endpoints.
BATCH_LIMIT = 100


class RobloxApiClient:
    """Typed wrapper around the public Roblox endpoints ScoutForge needs."""

    def __init__(
        self,
        http: PoliteHttpClient,
        *,
        api_base: str = "https://apis.roblox.com",
        games_base: str = "https://games.roblox.com",
        thumbnails_base: str = "https://thumbnails.roblox.com",
        users_base: str = "https://users.roblox.com",
    ) -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        self.games_base = games_base.rstrip("/")
        self.thumbnails_base = thumbnails_base.rstrip("/")
        self.users_base = users_base.rstrip("/")
        self.session_id = str(uuid.uuid4())
        self._creator_cache: dict[int, CreatorInfo | None] = {}

    def new_session_id(self) -> str:
        """Rotate the anonymous session id used by the Explore/Search APIs."""
        self.session_id = str(uuid.uuid4())
        return self.session_id

    # ------------------------------------------------------------ discovery
    async def home_page_sections(self) -> list[dict[str, Any]]:
        """Return the raw content sets behind roblox.com/discover.

        Each entry carries a ``setId`` which can be fed to
        :meth:`sort_content` to page through a curated feed.
        """
        url = f"{self.api_base}/explore-api/v1/get-home-page-details"
        try:
            payload = await self.http.get_json(url, params={"sessionId": self.session_id})
        except TransportError as exc:
            log_event(log, 30, "ROBLOX_HOME_PAGE_FAILED", error=str(exc)[:200])
            return []
        sections = payload.get("homePageDetails") if isinstance(payload, dict) else None
        return [s for s in (sections or []) if isinstance(s, dict)]

    async def sort_content(
        self, sort_id: str, *, page_token: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Fetch one page of a curated Explore feed."""
        url = f"{self.api_base}/explore-api/v1/get-sort-content"
        params: dict[str, Any] = {"sessionId": self.session_id, "sortId": sort_id}
        if page_token:
            params["pageToken"] = page_token
        del limit  # feed size is server controlled
        return await self.http.get_json(url, params=params)

    async def omni_search(
        self, query: str, *, page_token: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        """Keyword search across Roblox (``pageType=games``)."""
        url = f"{self.api_base}/search-api/omni-search"
        params: dict[str, Any] = {
            "searchQuery": query,
            "pageType": "all",
            "sessionId": session_id or self.session_id,
            "pageToken": page_token or "",
        }
        return await self.http.get_json(url, params=params)

    async def recommendations_for_universe(self, universe_id: int) -> list[dict[str, Any]]:
        """Return universes Roblox recommends alongside ``universe_id``."""
        url = f"{self.api_base}/recommendation-api/legacy/recommendations/universes/{universe_id}"
        try:
            payload = await self.http.get_json(url)
        except TransportError as exc:
            log_event(log, 30, "ROBLOX_RECO_ERROR", universe=universe_id, error=str(exc)[:200])
            return []
        results = payload.get("results") if isinstance(payload, dict) else None
        return [r for r in (results or []) if isinstance(r, dict)]

    # -------------------------------------------------------------- details
    async def games_v2(self, universe_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch fetch full game records (``/v2/games``)."""
        result: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(universe_ids, BATCH_LIMIT):
            url = f"{self.games_base}/v2/games"
            try:
                payload = await self.http.get_json(
                    url,
                    params={"universeIds": ",".join(str(i) for i in chunk)},
                    expected_status=(200, 400),
                )
            except TransportError as exc:
                log_event(log, 30, "ROBLOX_GAMES_V2_FAILED", count=len(chunk), error=str(exc)[:200])
                continue
            for record in _as_records(payload):
                universe_id = coerce_int(record.get("universeId"))
                if universe_id is not None:
                    result[universe_id] = record
        return result

    async def multiplayer(self, universe_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch fetch live player counts (``/v1/games/multiplayer``)."""
        result: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(universe_ids, BATCH_LIMIT):
            url = f"{self.games_base}/v1/games/multiplayer"
            try:
                payload = await self.http.get_json(
                    url,
                    params={"universeIds": ",".join(str(i) for i in chunk)},
                    expected_status=(200, 400),
                )
            except TransportError as exc:
                log_event(
                    log, 30, "ROBLOX_MULTIPLAYER_FAILED", count=len(chunk), error=str(exc)[:200]
                )
                continue
            for record in _as_records(payload):
                universe_id = coerce_int(record.get("universeId"))
                if universe_id is not None:
                    result[universe_id] = record
        return result

    async def thumbnails(self, universe_ids: list[int], *, size: str = "512x512") -> dict[int, str]:
        """Batch fetch thumbnail URLs for games."""
        result: dict[int, str] = {}
        for chunk in _chunks(universe_ids, BATCH_LIMIT):
            url = f"{self.thumbnails_base}/v1/games/icons"
            params = {
                "universeIds": ",".join(str(i) for i in chunk),
                "size": size,
                "format": "Png",
                "isCircular": "false",
            }
            try:
                payload = await self.http.get_json(url, params=params)
            except TransportError as exc:
                log_event(
                    log, 30, "ROBLOX_THUMBNAIL_FAILED", count=len(chunk), error=str(exc)[:160]
                )
                continue
            data = payload.get("data") if isinstance(payload, dict) else None
            for record in data or []:
                universe_id = coerce_int(record.get("targetId"))
                image_url = coerce_str(record.get("imageUrl")) if isinstance(record, dict) else None
                if universe_id is not None and image_url:
                    result[universe_id] = image_url
        return result

    async def creator_user(self, user_id: int) -> CreatorInfo | None:
        url = f"{self.users_base}/v1/users/{user_id}"
        try:
            payload = await self.http.get_json(url)
        except TransportError as exc:
            log_event(log, 30, "ROBLOX_USER_FAILED", user_id=user_id, error=str(exc)[:200])
            return None
        if not isinstance(payload, dict):
            return None
        return CreatorInfo(
            creator_id=coerce_int(payload.get("id")) or user_id,
            creator_name=coerce_str(payload.get("displayName"))
            or coerce_str(payload.get("username")),
            creator_type="User",
            creator_url=f"https://www.roblox.com/users/{user_id}/profile",
        )

    async def creator_group(self, group_id: int) -> CreatorInfo | None:
        url = f"{self.games_base}/v2/groups/{group_id}"
        try:
            payload = await self.http.get_json(url)
        except TransportError as exc:
            log_event(log, 30, "ROBLOX_GROUP_FAILED", group_id=group_id, error=str(exc)[:200])
            return None
        if not isinstance(payload, dict):
            return None
        return CreatorInfo(
            creator_id=coerce_int(payload.get("id")) or group_id,
            creator_name=coerce_str(payload.get("name")),
            creator_type="Group",
            group_id=coerce_int(payload.get("id")) or group_id,
            group_name=coerce_str(payload.get("name")),
            creator_url=f"https://www.roblox.com/groups/{group_id}",
        )

    async def resolve_creator(self, creator_id: int) -> CreatorInfo | None:
        """Resolve a creator to a ``User`` or ``Group``.

        ``/v2/games`` does not tell us the type, so we probe the group endpoint
        first (cheap 404 for users) and fall back to the user endpoint. Results
        are cached for the lifetime of the client because a creator id never
        changes type.
        """
        if creator_id in self._creator_cache:
            return self._creator_cache[creator_id]
        info = await self.creator_group(creator_id) or await self.creator_user(creator_id)
        self._creator_cache[creator_id] = info
        return info

    # ----------------------------------------------------------- enrichment
    async def get_details(self, universe_id: int) -> GameDetails | None:
        """Single-universe convenience wrapper around the batched lookups."""
        merged = await self.get_details_batch([universe_id])
        return merged.get(universe_id)

    async def get_details_batch(self, universe_ids: list[int]) -> dict[int, GameDetails]:
        """Fetch and merge details for many universes with only 3 requests per 100 ids.

        Metrics absent from the upstream payload are left as ``None`` and listed
        in :attr:`GameDetails.missing_metrics` - ScoutForge never guesses them.
        """
        if not universe_ids:
            return {}

        games = await self.games_v2(universe_ids)
        multiplayer = await self.multiplayer(universe_ids)
        thumbs = await self.thumbnails(universe_ids)

        merged: dict[int, GameDetails] = {}
        for universe_id in universe_ids:
            game = games.get(universe_id, {})
            multi = multiplayer.get(universe_id, {})
            if not game and not multi:
                continue
            details = _build_details(universe_id, game, multi, thumbs.get(universe_id))
            merged[universe_id] = details
        return merged


def _build_details(
    universe_id: int,
    game: dict[str, Any],
    multi: dict[str, Any],
    thumbnail_url: str | None,
) -> GameDetails:
    creator_raw: dict[str, Any] = game.get("creator") or {}
    creator_id = coerce_int(creator_raw.get("id"))
    creator_name = coerce_str(creator_raw.get("name"))
    has_verified_creator = coerce_bool(creator_raw.get("hasVerifiedBadge"))

    ccu = coerce_int(multi.get("playerCount"))
    if ccu is None:
        ccu = coerce_int(game.get("playing"))
    visits = coerce_int(game.get("visits"))
    if visits is None:
        visits = coerce_int(multi.get("visits"))
    favorites = coerce_int(game.get("favoritedCount"))
    likes = coerce_int(game.get("likeCount"))
    dislikes = coerce_int(game.get("dislikeCount"))

    name = coerce_str(game.get("name"), max_length=256)
    description = coerce_str(game.get("description"), max_length=4000)

    missing: list[str] = []
    if ccu is None:
        missing.append("current_ccu")
    if visits is None:
        missing.append("lifetime_visits")
    if favorites is None:
        missing.append("favorites")
    if likes is None:
        missing.append("likes")

    creator_type = _creator_type(creator_raw)
    creator = CreatorInfo(
        creator_id=creator_id,
        creator_name=creator_name,
        creator_type=creator_type,
        # Only build a URL when the type is known; otherwise it could point at a
        # user profile for what is really a group. ``resolve_creator`` fills it in.
        creator_url=(
            f"https://www.roblox.com/groups/{creator_id}"
            if creator_id and creator_type == "Group"
            else f"https://www.roblox.com/users/{creator_id}/profile"
            if creator_id and creator_type == "User"
            else None
        ),
    )
    if has_verified_creator is False and creator_name is None:
        missing.append("creator_name")

    return GameDetails(
        universe_id=universe_id,
        place_id=coerce_int(multi.get("placeId")) or coerce_int(game.get("rootPlaceId")),
        root_place_id=coerce_int(game.get("rootPlaceId")),
        name=name,
        description=description,
        creator=creator,
        current_ccu=ccu,
        lifetime_visits=visits,
        favorites=favorites,
        likes=likes,
        dislikes=dislikes,
        playing_now=coerce_int(game.get("playing")),
        max_players=coerce_int(multi.get("maxPlayers")) or coerce_int(game.get("maxPlayers")),
        all_genres=game.get("allGenre") if isinstance(game.get("allGenre"), list) else None,
        thumbnail_url=thumbnail_url,
        public_discord_url=extract_discord_url(description),
        created_at=coerce_timestamp(game.get("created")),
        updated_at=coerce_timestamp(game.get("updated")),
        is_private=coerce_bool(game.get("private")),
        missing_metrics=missing,
        raw={"game": game, "multiplayer": multi},
    )


def _creator_type(creator_raw: dict[str, Any]) -> str | None:
    """Read the creator type when the payload provides one.

    ``/v2/games`` returns only ``{id, name, hasVerifiedBadge}``, so the type is
    usually unknown at this point. :meth:`RobloxApiClient.resolve_creator`
    fills it in later, once per creator, and caches the answer.
    """
    if not creator_raw:
        return None
    ctype = coerce_str(creator_raw.get("creatorType")) or coerce_str(creator_raw.get("type"))
    if ctype and ctype.lower() in {"user", "group", "groupowned"}:
        return "Group" if ctype.lower().startswith("group") else "User"
    return None


def extract_discord_url(description: str | None) -> str | None:
    """Extract a Discord invite that the creator published in the game description.

    This only reads text the developer themselves published on the game's public
    Roblox page - no private data, no scraping of user profiles.
    """
    if not description:
        return None
    lowered = description.lower()
    for host in _DISCORD_HOSTS:
        index = lowered.find(host)
        if index == -1:
            continue
        start = description.rfind("http", 0, index)
        if start == -1:
            start = index
        tail = description[index : index + 128]
        url = (description[start:index] + tail).split()[0]
        url = "".join(ch for ch in url if ch not in _URL_TRIM_CHARS)
        if url.startswith("http") and len(url) <= 256:
            return url
    return None


def _as_records(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [record for record in data if isinstance(record, dict)]


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


__all__ = ["BATCH_LIMIT", "RobloxApiClient", "extract_discord_url"]
