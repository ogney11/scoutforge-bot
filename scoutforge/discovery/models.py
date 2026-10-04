"""Discovery domain models and payload validation.

These DTOs are the *only* shape the rest of the application understands, so a
new data source only has to emit :class:`GameCandidate` objects. Nothing here
assumes a metric exists: unavailable values stay ``None`` and are reported
through :attr:`GameCandidate.missing_metrics`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scoutforge.utils import parse_roblox_timestamp


@dataclass(slots=True)
class CreatorInfo:
    """Details about the user or group behind a game."""

    creator_id: int | None = None
    creator_name: str | None = None
    creator_type: str | None = None  # "User" | "Group"
    group_id: int | None = None
    group_name: str | None = None
    creator_url: str | None = None


@dataclass(slots=True)
class GameDetails:
    """Full metric payload for a single universe."""

    universe_id: int
    place_id: int | None = None
    name: str | None = None
    description: str | None = None
    creator: CreatorInfo = field(default_factory=CreatorInfo)
    current_ccu: int | None = None
    lifetime_visits: int | None = None
    favorites: int | None = None
    likes: int | None = None
    dislikes: int | None = None
    playing_now: int | None = None
    voice_chat_visits: int | None = None
    max_players: int | None = None
    all_genres: list[int] | None = None
    thumbnail_url: str | None = None
    public_discord_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    is_private: bool | None = None
    root_place_id: int | None = None
    missing_metrics: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def roblox_url(self) -> str:
        if self.place_id:
            return f"https://www.roblox.com/games/{self.place_id}/{_slug(self.name)}"
        return f"https://www.roblox.com/games/{self.universe_id}"

    @property
    def ccu_to_visits_ratio(self) -> float | None:
        if not self.current_ccu or not self.lifetime_visits:
            return None
        return self.current_ccu / self.lifetime_visits


@dataclass(slots=True)
class GameCandidate:
    """A cheap, un-enriched candidate produced by a discovery source.

    Discovery endpoints rarely expose CCU directly, so a candidate usually only
    carries ``universe_id`` and a few hints. The worker enriches candidates
    through :meth:`~scoutforge.discovery.base.GameDiscoverySource.get_game_details`
    before filtering on real metrics.
    """

    universe_id: int
    source: str
    place_id: int | None = None
    name: str | None = None
    creator_id: int | None = None
    creator_name: str | None = None
    current_ccu: int | None = None
    lifetime_visits: int | None = None
    favorites: int | None = None
    thumbnail_url: str | None = None
    discovery_rank: int | None = None
    discovery_context: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return str(self.universe_id)


@dataclass(slots=True)
class DiscoveryBatch:
    """Result of one ``discover_games`` call."""

    source: str
    candidates: list[GameCandidate] = field(default_factory=list)
    pages_fetched: int = 0
    errors: int = 0
    notes: str | None = None

    def __len__(self) -> int:
        return len(self.candidates)


# ---------------------------------------------------------------------------
# Coercion helpers - defensive parsing of untrusted upstream JSON
# ---------------------------------------------------------------------------
def coerce_int(value: Any) -> int | None:
    """Best-effort conversion of an upstream value to a non-negative int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = int(float(text))
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def coerce_str(value: Any, *, max_length: int = 512) -> str | None:
    """Best-effort conversion to a bounded, non-empty string."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_length]


def coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def coerce_timestamp(value: Any) -> datetime | None:
    """Parse either an ISO-8601 string or a unix timestamp."""
    parsed = parse_roblox_timestamp(value)
    if parsed is not None:
        return parsed
    epoch = coerce_int(value)
    if epoch is None or epoch <= 0:
        return None
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _slug(name: str | None) -> str:
    if not name:
        return "unnamed"
    return "".join(ch if ch.isalnum() else "-" for ch in name)[:80].strip("-") or "unnamed"


__all__ = [
    "CreatorInfo",
    "DiscoveryBatch",
    "GameCandidate",
    "GameDetails",
    "coerce_bool",
    "coerce_int",
    "coerce_str",
    "coerce_timestamp",
]
