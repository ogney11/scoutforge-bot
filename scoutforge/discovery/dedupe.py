"""Deduplication of candidates within and across discovery sources.

Roblox universes can be surfaced many times per scan (multiple Explore feeds,
several keyword hits, a recommendation feed). ScoutForge merges everything on
``universe_id`` - the only stable identity a game has.

Near-duplicates are also detected by normalised name + creator, because the same
re-uploaded or re-themed game frequently appears under a new universe id. Those
are *flagged*, never silently deleted, because a false merge loses history.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from scoutforge.discovery.models import GameCandidate

_PUNCTUATION = re.compile(r"[^a-z0-9]+")
_SPACES = re.compile(r"\s+")


def normalize_game_name(name: str | None) -> str:
    """Lowercase, strip accents and punctuation for fuzzy comparison."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_text.lower()
    collapsed = _PUNCTUATION.sub(" ", lowered)
    return _SPACES.sub(" ", collapsed).strip()


@dataclass(slots=True)
class DedupeResult:
    """Merge outcome for one universe."""

    universe_id: int
    candidates: list[GameCandidate] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)

    @property
    def primary(self) -> GameCandidate:
        """The best candidate: richest metrics, then earliest discovery rank."""
        return max(
            self.candidates,
            key=lambda c: (
                c.current_ccu is not None,
                c.lifetime_visits is not None,
                c.name is not None,
                -(c.discovery_rank if c.discovery_rank is not None else 10**6),
            ),
        )

    @property
    def source_count(self) -> int:
        return len(self.sources)


def merge_candidates(candidates: Iterable[GameCandidate]) -> dict[int, DedupeResult]:
    """Group candidates by universe id, keeping the richest payload per group."""
    merged: dict[int, DedupeResult] = {}
    for candidate in candidates:
        if not candidate.universe_id or candidate.universe_id <= 0:
            continue
        entry = merged.setdefault(candidate.universe_id, DedupeResult(candidate.universe_id))
        entry.candidates.append(candidate)
        entry.sources.add(candidate.source)
    return merged


def find_near_duplicates(
    candidates: Mapping[int, DedupeResult],
    *,
    min_name_length: int = 4,
) -> dict[int, list[int]]:
    """Return ``universe_id -> [other_universe_ids]`` for likely re-uploads.

    Two games are considered near-duplicates when their normalised names match
    exactly *and* they share a creator id (when both are known). Callers decide
    what to do with the result - ScoutForge only records it.
    """
    buckets: dict[tuple[str, int | None], list[int]] = {}
    for universe_id, entry in candidates.items():
        primary = entry.primary
        normalized = normalize_game_name(primary.name)
        if len(normalized) < min_name_length:
            continue
        buckets.setdefault((normalized, primary.creator_id), []).append(universe_id)

    duplicates: dict[int, list[int]] = {}
    for (normalized, _creator), ids in buckets.items():
        del normalized
        if len(ids) > 1:
            for universe_id in ids:
                duplicates[universe_id] = [other for other in ids if other != universe_id]
    return duplicates


class Deduplicator:
    """Stateful helper that merges candidates across a whole scan."""

    def __init__(self) -> None:
        self._merged: dict[int, DedupeResult] = {}

    def add_all(self, candidates: Iterable[GameCandidate]) -> None:
        for universe_id, entry in merge_candidates(candidates).items():
            existing = self._merged.get(universe_id)
            if existing is None:
                self._merged[universe_id] = entry
                continue
            existing.candidates.extend(entry.candidates)
            existing.sources |= entry.sources

    def add(self, candidate: GameCandidate) -> None:
        if not candidate.universe_id or candidate.universe_id <= 0:
            return
        entry = self._merged.setdefault(candidate.universe_id, DedupeResult(candidate.universe_id))
        entry.candidates.append(candidate)
        entry.sources.add(candidate.source)

    @property
    def results(self) -> dict[int, DedupeResult]:
        return self._merged

    @property
    def universe_ids(self) -> list[int]:
        return list(self._merged)

    def near_duplicates(self) -> dict[int, list[int]]:
        return find_near_duplicates(self._merged)

    def __len__(self) -> int:
        return len(self._merged)

    def __bool__(self) -> bool:
        return bool(self._merged)


__all__ = [
    "DedupeResult",
    "Deduplicator",
    "find_near_duplicates",
    "merge_candidates",
    "normalize_game_name",
]
