"""The Scout Score calculator.

Design rules:
  * **Transparent** - every point is attributable to a named component with a
    written reason, and the reasons are stored next to the score.
  * **Honest** - components whose inputs are unavailable score 0 *and* add a
    caveat; the remaining weight is redistributed proportionally so a game with
    only two known metrics is not structurally penalised for the missing ones.
  * **Deterministic** - no randomness, no clock reads except the injectable
    ``now``, so the same inputs always produce the same score.
  * **Not a prediction** - it measures observed, current attractiveness only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import AlertSettings, ScoreWeights, get_settings
from scoutforge.enums import AlertLevel
from scoutforge.scoring.rules import COMPONENTS, ComponentResult, ScoreInput
from scoutforge.utils import utcnow

log = get_logger(__name__)

#: Shown next to every score so nobody misreads it as a forecast.
SCORE_DISCLAIMER = (
    "Scout Score measures current observed attractiveness against the configured "
    "criteria. It is not a prediction of commercial success."
)


@dataclass(slots=True)
class ScoutScore:
    """A fully explained 0-100 score."""

    universe_id: int
    score: float
    level: AlertLevel
    components: list[ComponentResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=utcnow)
    available_weight: int = 0

    @property
    def positives(self) -> list[str]:
        return list(self.reasons)

    def component_map(self) -> dict[str, dict[str, Any]]:
        return {
            component.key: {
                "label": component.label,
                "weight": component.weight,
                "points": component.points,
                "value": round(component.value, 4),
                "available": component.available,
                "detail": component.detail,
            }
            for component in self.components
        }

    def to_row(self) -> dict[str, Any]:
        """Flatten into a ``game_scores`` row."""
        by_key = {component.key: component for component in self.components}
        ratio_detail = by_key["ratio"].detail if by_key["ratio"].available else {}
        growth_detail = by_key["growth"].detail if by_key["growth"].available else {}
        return {
            "universe_id": self.universe_id,
            "score": round(self.score, 2),
            "level": self.level.value,
            "computed_at": self.computed_at,
            "components": self.component_map(),
            "reasons": self.reasons,
            "caveats": self.caveats,
            "ccu_to_visits_ratio": ratio_detail.get("ratio"),
            "growth_24h_pct": growth_detail.get("pct_change")
            if growth_detail.get("window_hours") == 24
            else None,
        }


class ScoutScorer:
    """Computes the transparent 0-100 Scout Score."""

    def __init__(
        self,
        weights: ScoreWeights | None = None,
        alerts: AlertSettings | None = None,
    ) -> None:
        settings = get_settings()
        self.weights = weights or settings.score
        self.alerts = alerts or settings.alerts

    # ------------------------------------------------------------------ api
    def score(self, data: ScoreInput, *, now: datetime | None = None) -> ScoutScore:
        """Score one game."""
        now = now or data.now or utcnow()
        components = [rule(data, self.weights) for rule in COMPONENTS]

        available = [c for c in components if c.available]
        available_weight = sum(c.weight for c in available)

        if available_weight <= 0:
            score = 0.0
            caveats = ["No usable metrics - cannot score"]
        else:
            earned = sum(c.points for c in available)
            # Redistribute the missing weight across the components we *can* score.
            score = _clamp(earned / available_weight * 100.0)

        reasons = _rank_reasons(available)
        caveats = _collect_caveats(components, data)
        if not available:
            # Lead with the reason the score is 0: "no data" must never be
            # indistinguishable from "measured, and it is bad".
            caveats.insert(0, "No usable metrics - cannot score")

        result = ScoutScore(
            universe_id=data.universe_id,
            score=round(score, 2),
            level=self.level_for(score),
            components=components,
            reasons=reasons,
            caveats=caveats,
            computed_at=now,
            available_weight=available_weight,
        )

        log_event(
            log,
            20,
            "SCORE",
            universe=data.universe_id,
            score=result.score,
            alert_level=result.level.value,
            available_weight=available_weight,
        )
        return result

    def score_many(
        self, items: list[ScoreInput], *, now: datetime | None = None
    ) -> list[ScoutScore]:
        return [self.score(item, now=now) for item in items]

    def level_for(self, score: float) -> AlertLevel:
        """Map a score to an alert level using the configured thresholds."""
        if score >= self.alerts.extreme_score:
            return AlertLevel.EXTREME
        if score >= self.alerts.hot_score:
            return AlertLevel.HOT
        if score >= self.alerts.warm_score:
            return AlertLevel.WARM
        return AlertLevel.NORMAL

    def describe(self, result: ScoutScore) -> str:
        """Render the human-readable explanation block."""
        lines = [f"Scout Score: {result.score:.0f}/100 ({result.level.value})", "", "Reasons:"]
        lines.extend(f"• {reason}" for reason in result.reasons or ["No positive signals yet"])
        if result.caveats:
            lines.extend(["", "Limitations:"])
            lines.extend(f"• {caveat}" for caveat in result.caveats)
        lines.extend(["", SCORE_DISCLAIMER])
        return "\n".join(lines)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _collect_caveats(components: list[ComponentResult], data: ScoreInput) -> list[str]:
    caveats: list[str] = list(data.growth.caveats) if data.growth and data.growth.caveats else []
    for component in components:
        if not component.available:
            caveats.extend(component.caveats)
    if data.snapshot_count < 2:
        caveats.append("Limited historical data (single observation)")
    seen: set[str] = set()
    unique: list[str] = []
    for caveat in caveats:
        if caveat not in seen:
            seen.add(caveat)
            unique.append(caveat)
    return unique


def _rank_reasons(components: list[ComponentResult]) -> list[str]:
    """Order reasons by how much each component actually contributed."""
    ordered = sorted(
        components, key=lambda c: c.points / c.weight if c.weight else 0.0, reverse=True
    )
    reasons: list[str] = []
    for component in ordered:
        if component.available:
            reasons.extend(component.reasons)
    return reasons[:6]


__all__ = ["COMPONENTS", "SCORE_DISCLAIMER", "ScoreInput", "ScoutScore", "ScoutScorer"]
