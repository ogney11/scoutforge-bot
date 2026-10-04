"""Component rules for the Scout Score.

Every rule is *measurable* and *bounded*: it takes known inputs and returns a
``0..1`` value plus the human-readable reasons and caveats that justify it.

The score is explicitly **not** a prediction. It answers one question only:
"how interesting is this game right now, relative to the scout's stated
criteria, given the data ScoutForge has actually collected?"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from scoutforge.partners.keywords import extract_signals
from scoutforge.utils import ensure_utc, safe_div, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from scoutforge.config.settings import ScoreWeights
    from scoutforge.monitoring.growth import GrowthMetrics


@dataclass(slots=True)
class ScoreInput:
    """Everything the scorer is allowed to look at.

    Every field is optional: an unavailable metric is ``None`` and the matching
    component is redistributed proportionally across the remaining components
    (or zero, when nothing is known) instead of being faked.
    """

    universe_id: int
    name: str | None = None
    description: str | None = None
    current_ccu: int | None = None
    lifetime_visits: int | None = None
    favorites: int | None = None
    likes: int | None = None
    dislikes: int | None = None
    first_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    peak_ccu: int | None = None
    snapshot_count: int = 0
    growth: GrowthMetrics | None = None
    now: datetime | None = None

    @property
    def ccu_to_visits_ratio(self) -> float | None:
        return safe_div(self.current_ccu, self.lifetime_visits)

    @property
    def like_ratio(self) -> float | None:
        """Likes as a share of lifetime visits - a crude engagement proxy."""
        return safe_div(self.likes, self.lifetime_visits)

    @property
    def favorite_ratio(self) -> float | None:
        return safe_div(self.favorites, self.lifetime_visits)


@dataclass(slots=True)
class ComponentResult:
    """One scored dimension."""

    key: str
    label: str
    weight: int
    value: float  # 0..1
    points: float  # 0..weight
    available: bool
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _points(value: float, weight: int) -> float:
    return round(_clamp(value) * weight, 2)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def score_ccu(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Absolute live player count, log-scaled so 1k CCU already earns credit."""
    result = ComponentResult("ccu", "Live CCU", weights.ccu, 0.0, 0.0, available=False)
    if data.current_ccu is None:
        result.caveats.append("CCU unavailable")
        return result
    result.available = True
    reference = max(1.0, weights.ccu_reference)
    # log10 scaling: CCU 1k -> 0.4 of reference band, 10k -> 1.0
    normalized = math.log10(max(data.current_ccu, 1) + 1) / math.log10(reference + 1)
    result.value = _clamp(normalized)
    result.points = _points(result.value, weights.ccu)
    result.detail = {"current_ccu": data.current_ccu, "reference": reference}
    if data.current_ccu >= weights.ccu_reference:
        result.reasons.append(f"Strong live audience of {data.current_ccu:,} concurrent players")
    elif data.current_ccu >= 1000:
        result.reasons.append(f"Healthy live audience of {data.current_ccu:,} CCU")
    else:
        result.reasons.append(f"Live audience of {data.current_ccu:,} CCU")
    if data.peak_ccu:
        result.detail["peak_ccu"] = data.peak_ccu
    return result


def score_ratio(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """CCU / lifetime visits - the core "under-followed but hot" signal."""
    result = ComponentResult("ratio", "CCU per visit", weights.ratio, 0.0, 0.0, available=False)
    ratio = data.ccu_to_visits_ratio
    if ratio is None:
        result.caveats.append("CCU/lifetime-visits ratio unavailable")
        return result
    result.available = True
    reference = max(1e-6, weights.ratio_reference)
    result.value = _clamp(ratio / reference)
    result.points = _points(result.value, weights.ratio)
    pct = ratio * 100
    result.detail = {"ratio": round(ratio, 6), "percent": round(pct, 3), "reference": reference}
    if pct >= 1.0:
        result.reasons.append(
            f"Very high CCU relative to lifetime visits ({pct:.2f}% of all-time visits)"
        )
    elif pct >= 0.5:
        result.reasons.append(f"High CCU relative to lifetime visits ({pct:.2f}%)")
    elif pct >= 0.2:
        result.reasons.append(f"Healthy CCU-to-visits ratio ({pct:.2f}%)")
    else:
        result.reasons.append(f"Modest CCU-to-visits ratio ({pct:.2f}%)")
    return result


def score_growth(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Recent CCU growth, preferring 24h data but degrading gracefully."""
    result = ComponentResult("growth", "CCU growth", weights.growth, 0.0, 0.0, available=False)
    growth = data.growth
    if growth is None or not growth.has_growth_data:
        result.caveats.append("Limited historical data - growth not yet measurable")
        return result

    chosen = growth.primary_growth
    if chosen is None:
        result.caveats.append("No comparable historical snapshot yet")
        return result

    result.available = True
    reference = max(1e-6, weights.growth_reference)  # 100% growth == full marks
    value = chosen.pct_change / 100.0
    result.value = _clamp(value / reference)
    result.points = _points(result.value, weights.growth)
    result.detail = {
        "window_hours": chosen.window_hours,
        "pct_change": round(chosen.pct_change, 2),
        "absolute_change": chosen.absolute_change,
        "samples": chosen.samples,
    }
    if chosen.pct_change >= 100:
        result.reasons.append(
            f"Doubling or more in ~{chosen.window_hours}h ({chosen.pct_change:+.0f}% CCU)"
        )
    elif chosen.pct_change >= 40:
        result.reasons.append(
            f"Strong CCU growth of {chosen.pct_change:+.0f}% over ~{chosen.window_hours}h"
        )
    elif chosen.pct_change >= 15:
        result.reasons.append(
            f"Steady CCU growth of {chosen.pct_change:+.0f}% over ~{chosen.window_hours}h"
        )
    elif chosen.pct_change >= 0:
        result.reasons.append(f"Slight CCU growth of {chosen.pct_change:+.0f}%")
    else:
        result.reasons.append(
            f"CCU is declining ({chosen.pct_change:+.0f}% over ~{chosen.window_hours}h)"
        )
    return result


def score_freshness(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Newly emerging games score high; long-known games fade."""
    result = ComponentResult("freshness", "Freshness", weights.freshness, 0.0, 0.0, available=False)
    if data.first_seen_at is None:
        result.caveats.append("First-seen timestamp unavailable")
        return result
    now = ensure_utc(data.now) or utcnow()
    age_days = max(0.0, (now - ensure_utc(data.first_seen_at)).total_seconds() / 86400.0)  # type: ignore[operator]
    result.available = True
    result.value = _clamp(1.0 - (age_days / max(1, weights.freshness_days)))
    result.points = _points(result.value, weights.freshness)
    result.detail = {"age_days": round(age_days, 2)}
    if age_days <= 3:
        result.reasons.append(f"Newly detected by ScoutForge {age_days:.1f} days ago")
    elif age_days <= 14:
        result.reasons.append(f"Recently detected ({age_days:.0f} days of tracking)")
    else:
        result.reasons.append(f"Tracked for {age_days:.0f} days")

    if data.created_at:
        game_age = (now - ensure_utc(data.created_at)).total_seconds() / 86400.0  # type: ignore[operator]
        result.detail["game_age_days"] = round(game_age, 2)
        if game_age <= 60:
            result.reasons.append(f"Game itself is new (~{game_age:.0f} days old)")
    return result


def score_consistency(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """How steadily the audience holds up, derived from observed snapshots only."""
    result = ComponentResult(
        "consistency", "Audience consistency", weights.consistency, 0.0, 0.0, available=False
    )
    growth = data.growth
    if growth is None or not growth.has_consistency_data:
        result.caveats.append("Not enough snapshots to judge activity consistency")
        return result
    stability = growth.stability
    if stability is None:  # pragma: no cover - has_consistency_data guarantees this
        result.caveats.append("Not enough snapshots to judge activity consistency")
        return result

    result.available = True
    result.value = _clamp(stability)
    result.points = _points(result.value, weights.consistency)
    result.detail = {
        "stability": round(stability, 3),
        "samples": growth.consistency_samples,
        "window_hours": weights.consistency_window_hours,
    }
    if stability >= 0.9:
        result.reasons.append(
            f"Very stable player activity across {growth.consistency_samples} observations"
        )
    elif stability >= 0.7:
        result.reasons.append(
            f"Consistent player activity across {growth.consistency_samples} observations"
        )
    elif stability >= 0.5:
        result.reasons.append(
            f"Moderately stable activity across {growth.consistency_samples} observations"
        )
    else:
        result.reasons.append("Erratic player activity - treat CCU as a spike, not a baseline")
    return result


def score_footprint(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Reward *small-to-medium* total footprint: tiny games have room, but so do
    proven ones.

    The component is deliberately gentle and has real headroom at both ends. An
    earlier version log-scaled against 500k visits and gave an established
    millions-of-visits game a hard zero, which made large but healthy titles
    unscoreable. Now the curve only fades off past ~50M visits, so a 40M-visit
    game with a strong CCU still earns most of the footprint points.
    """
    result = ComponentResult(
        "footprint", "Lifetime footprint", weights.footprint, 0.0, 0.0, available=False
    )
    if data.lifetime_visits is None:
        result.caveats.append("Lifetime visits unavailable")
        return result
    result.available = True
    # Full marks up to 500k visits, fading logarithmically to ~0 at 50M.
    ceiling = 50_000_000.0
    normalized = math.log10(max(data.lifetime_visits, 1) + 1) / math.log10(ceiling + 1)
    floor = math.log10(500_000 + 1) / math.log10(ceiling + 1)
    # Re-map so the 500k band is exactly 1.0 instead of ~0.5.
    value = 1.0 if normalized <= floor else 1.0 - (normalized - floor) / (1.0 - floor)
    result.value = _clamp(value)
    result.points = _points(result.value, weights.footprint)
    result.detail = {"lifetime_visits": data.lifetime_visits}
    if data.lifetime_visits <= 50_000:
        result.reasons.append(f"Very small lifetime footprint ({data.lifetime_visits:,} visits)")
    elif data.lifetime_visits <= 500_000:
        result.reasons.append(f"Low lifetime visit footprint ({data.lifetime_visits:,} visits)")
    elif result.value >= 0.6:
        result.reasons.append(
            f"Established audience of {data.lifetime_visits:,} lifetime visits "
            "(proved demand, still scalable)"
        )
    else:
        result.reasons.append(f"Very large lifetime footprint ({data.lifetime_visits:,} visits)")
    return result


def score_monetization(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """How much evidence there is that a game actually earns.

    Roblox does not publish revenue, so this is explicitly a *signal* score built
    from two things we can measure: accumulated engagement (which only exists if
    players have spent time) and monetisation vocabulary in the title/description
    (gamepass, VIP, tycoon, boosters...). It is never presented as revenue.
    """
    result = ComponentResult(
        "monetization", "Monetisation signals", weights.monetization, 0.0, 0.0, available=False
    )
    reference = max(1.0, weights.monetization_visits_reference)
    engagement: float | None = None
    if data.lifetime_visits and (data.likes or data.favorites):
        engagement = (data.likes or 0) + (data.favorites or 0)
    keyword_hits = 0
    if data.description or data.name:
        signals = extract_signals(data.name, data.description)
        keyword_hits = signals.monetization_hint_count

    if engagement is None and keyword_hits == 0:
        result.caveats.append("No monetisation signals published by the source")
        return result

    result.available = True
    engagement_value = (
        _clamp(math.log10(engagement + 1) / math.log10(reference + 1)) if engagement else 0.0
    )
    keyword_value = _clamp(keyword_hits / 4.0)
    result.value = _clamp(0.6 * engagement_value + 0.4 * keyword_value)
    result.points = _points(result.value, weights.monetization)
    result.detail = {
        "engagement_actions": engagement,
        "keyword_hits": keyword_hits,
        "reference_visits": reference,
    }
    if engagement is not None:
        result.reasons.append(
            f"{engagement:,} likes+favourites accumulated, the usual footprint of paying players"
        )
    if keyword_hits:
        result.reasons.append(f"{keyword_hits} monetisation keyword(s) in the title/description")
    result.caveats.append("Revenue is never published by Roblox; this is a signal, not earnings")
    return result


def score_originality(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Penalise obvious genre reskins and flag risky content.

    A copy or a mount-only simulator is not a good partner target and carries
    DMCA/asset risk, so the score says so. Partner-specific exclusions live in
    :mod:`scoutforge.partners`; this component is the platform-wide opinion.
    """
    result = ComponentResult(
        "originality", "Originality", weights.originality, 0.0, 0.0, available=False
    )
    if not (data.name or data.description):
        result.caveats.append("No title or description to judge originality from")
        return result
    signals = extract_signals(data.name, data.description)
    result.available = True
    risk_penalty = min(0.75, 0.3 * len(signals.risk_keywords))
    clone_penalty = min(0.35, 0.12 * len(signals.originality_keywords))
    result.value = _clamp(1.0 - risk_penalty - clone_penalty)
    result.points = _points(result.value, weights.originality)
    result.detail = {
        "risk_keywords": list(signals.risk_keywords),
        "genre_hints": list(signals.originality_keywords),
    }
    if signals.risk_keywords:
        result.reasons.append("Risk markers in the title: " + ", ".join(signals.risk_keywords[:4]))
    elif signals.originality_keywords:
        result.reasons.append(
            "Genre-clone wording ("
            + ", ".join(signals.originality_keywords[:3])
            + ") - originality hard to judge"
        )
    else:
        result.reasons.append("No copy/clone or genre-clone markers detected")
    return result


def score_engagement(data: ScoreInput, weights: ScoreWeights) -> ComponentResult:
    """Optional engagement metrics (favorites, likes). Never invented."""
    result = ComponentResult(
        "engagement", "Engagement", weights.engagement, 0.0, 0.0, available=False
    )
    signals: list[float] = []
    if data.favorites is not None and data.lifetime_visits:
        ratio = data.favorite_ratio
        if ratio is not None:
            signals.append(_clamp(ratio / 0.02))
            result.detail["favorite_ratio"] = round(ratio, 6)
    if data.likes is not None and data.lifetime_visits:
        ratio = data.like_ratio
        if ratio is not None:
            signals.append(_clamp(ratio / 0.02))
            result.detail["like_ratio"] = round(ratio, 6)
    if not signals:
        result.caveats.append("No engagement metrics published by the source")
        return result

    result.available = True
    result.value = _clamp(sum(signals) / len(signals))
    result.points = _points(result.value, weights.engagement)
    if data.favorites:
        result.detail["favorites"] = data.favorites
    if data.likes:
        result.detail["likes"] = data.likes
    if data.favorites and data.likes:
        result.reasons.append(
            f"{data.favorites:,} favourites / {data.likes:,} likes recorded by the platform"
        )
    elif data.favorites:
        result.reasons.append(f"{data.favorites:,} favourites recorded by the platform")
    elif data.likes:
        result.reasons.append(f"{data.likes:,} likes recorded by the platform")
    return result


#: Ordered component list. Order is the order shown in the score explanation.
COMPONENTS = (
    score_ratio,
    score_ccu,
    score_growth,
    score_freshness,
    score_consistency,
    score_footprint,
    score_engagement,
    score_monetization,
    score_originality,
)


__all__ = [
    "COMPONENTS",
    "ComponentResult",
    "ScoreInput",
    "score_ccu",
    "score_consistency",
    "score_engagement",
    "score_footprint",
    "score_freshness",
    "score_growth",
    "score_monetization",
    "score_originality",
    "score_ratio",
]
