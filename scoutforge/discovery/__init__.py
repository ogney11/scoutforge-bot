"""Discovery subsystem: pluggable sources, hard filters, dedupe and enrichment."""

from scoutforge.discovery.base import (
    EnrichmentCapableSource,
    GameDiscoverySource,
    SeededDiscoverySource,
    candidate_from_details,
)
from scoutforge.discovery.dedupe import (
    DedupeResult,
    Deduplicator,
    find_near_duplicates,
    merge_candidates,
    normalize_game_name,
)
from scoutforge.discovery.enrichment import GameEnricher
from scoutforge.discovery.filters import FilterVerdict, GameFilter, RejectionReason
from scoutforge.discovery.models import (
    CreatorInfo,
    DiscoveryBatch,
    GameCandidate,
    GameDetails,
)
from scoutforge.discovery.registry import (
    SourceBundle,
    available_sources,
    build_sources,
    register,
    register_source,
)

__all__ = [
    "CreatorInfo",
    "DedupeResult",
    "Deduplicator",
    "DiscoveryBatch",
    "EnrichmentCapableSource",
    "FilterVerdict",
    "GameCandidate",
    "GameDetails",
    "GameDiscoverySource",
    "GameEnricher",
    "GameFilter",
    "RejectionReason",
    "SeededDiscoverySource",
    "SourceBundle",
    "available_sources",
    "build_sources",
    "candidate_from_details",
    "find_near_duplicates",
    "merge_candidates",
    "normalize_game_name",
    "register",
    "register_source",
]
