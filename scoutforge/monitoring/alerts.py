"""Alert level classification and cooldown bookkeeping.

The engine decides *whether* to notify; it never touches Discord itself (that is
:mod:`scoutforge.bot.alerts`). Keeping the decision pure makes it trivially
testable and lets the dashboard replay history with the same logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from scoutforge.config.settings import AlertSettings
from scoutforge.enums import AlertLevel
from scoutforge.monitoring.growth import GrowthMetrics
from scoutforge.utils import seconds_since, utcnow


class SuppressionReason(StrEnum):
    """Why a would-be alert was not sent."""

    BELOW_WARM_THRESHOLD = "below_warm_threshold"
    COOLDOWN = "cooldown"
    DISABLED = "alerts_disabled"
    NOT_NOTIFIABLE = "level_not_notifiable"
    SCORE_JUMP = "score_jump"  # overrides cooldown -> alert IS sent
    CCU_SPIKE = "ccu_spike"  # overrides cooldown -> alert IS sent


@dataclass(slots=True)
class AlertDecision:
    """Outcome of evaluating one game against the alert rules."""

    level: AlertLevel
    should_notify: bool
    reason: str
    score: float
    cooldown_seconds: int = 0
    cooldown_expires_at: datetime | None = None
    score_delta: float | None = None
    ccu_spike_pct: float | None = None

    @property
    def is_override(self) -> bool:
        """True when the alert bypassed an active cooldown.

        Covers every reason that delivers *despite* a live cooldown: a score
        jump, a CCU spike, a growth-driven promotion, and a plain escalation to
        a stronger level.
        """
        return self.reason in (
            SuppressionReason.SCORE_JUMP.value,
            SuppressionReason.CCU_SPIKE.value,
            "level_escalated",
        )


def level_for_score(score: float, settings: AlertSettings) -> AlertLevel:
    """Pure threshold mapping (used by both the scorer and the alert engine)."""
    if score >= settings.extreme_score:
        return AlertLevel.EXTREME
    if score >= settings.hot_score:
        return AlertLevel.HOT
    if score >= settings.warm_score:
        return AlertLevel.WARM
    return AlertLevel.NORMAL


def cooldown_for(level: AlertLevel, settings: AlertSettings) -> int:
    if level is AlertLevel.EXTREME:
        return settings.extreme_cooldown_seconds
    if level is AlertLevel.HOT:
        return settings.hot_cooldown_seconds
    return settings.warm_cooldown_seconds


@dataclass(slots=True)
class LastAlertState:
    """The most recent *delivered* alert for a game, as loaded from the DB."""

    created_at: datetime
    level: AlertLevel
    score: float
    ccu: int | None = None
    warnings: list[str] = field(default_factory=list)


class AlertEngine:
    """Decides alert level, delivery and cooldown suppression."""

    def __init__(self, settings: AlertSettings | None = None) -> None:
        self.settings = settings or AlertSettings()

    def evaluate(
        self,
        *,
        score: float,
        growth: GrowthMetrics | None = None,
        last_alert: LastAlertState | None = None,
        current_ccu: int | None = None,
        now: datetime | None = None,
        alerts_enabled: bool | None = None,
    ) -> AlertDecision:
        """Classify a game and decide whether to notify.

        An alert is sent when:
          * alerts are enabled, **and**
          * the level is HOT or EXTREME, **and**
          * no alert was delivered within the level's cooldown, **or**
          * the score jumped by at least ``ALERT_SCORE_JUMP_THRESHOLD`` points,
            **or**
          * CCU spiked by at least ``ALERT_CCU_SPIKE_PCT`` percent.

        The EXTREME-by-growth rule uses the *observed* growth window; a game
        cannot reach EXTREME on growth that ScoutForge has not actually measured.
        """
        now = now or utcnow()
        enabled = self.settings.enabled if alerts_enabled is None else alerts_enabled

        level = level_for_score(score, self.settings)
        growth_spike = self._extreme_growth_override(growth)

        if growth_spike is not None:
            level = AlertLevel.EXTREME

        if not enabled:
            return AlertDecision(
                level=AlertLevel.NORMAL,
                should_notify=False,
                reason=SuppressionReason.DISABLED.value,
                score=score,
            )

        if not level.notifies_discord:
            return AlertDecision(
                level=level,
                should_notify=False,
                reason=SuppressionReason.BELOW_WARM_THRESHOLD.value,
                score=score,
            )

        cooldown = cooldown_for(level, self.settings)
        score_delta = (score - last_alert.score) if last_alert else None
        ccu_spike = self._ccu_spike(last_alert.ccu if last_alert else None, current_ccu)

        if last_alert is None:
            return AlertDecision(
                level=level,
                should_notify=True,
                reason="first_alert",
                score=score,
                cooldown_seconds=cooldown,
                score_delta=score_delta,
                ccu_spike_pct=ccu_spike,
            )

        age = seconds_since(last_alert.created_at, now=now)

        # A stronger alert level always breaks the cooldown: EXTREME must be
        # seen even when HOT fired an hour ago.
        if level.rank > last_alert.level.rank:
            return AlertDecision(
                level=level,
                should_notify=True,
                reason=SuppressionReason.CCU_SPIKE.value
                if growth_spike is not None
                else "level_escalated",
                score=score,
                cooldown_seconds=cooldown,
                cooldown_expires_at=None,
                score_delta=score_delta,
                ccu_spike_pct=growth_spike if growth_spike is not None else ccu_spike,
            )

        if cooldown <= 0 or age is None or age >= cooldown:
            return AlertDecision(
                level=level,
                should_notify=True,
                reason="cooldown_expired",
                score=score,
                cooldown_seconds=cooldown,
                cooldown_expires_at=last_alert.created_at,
                score_delta=score_delta,
                ccu_spike_pct=ccu_spike,
            )

        if score_delta is not None and score_delta >= self.settings.score_jump_threshold:
            return AlertDecision(
                level=level,
                should_notify=True,
                reason=SuppressionReason.SCORE_JUMP.value,
                score=score,
                cooldown_seconds=cooldown,
                cooldown_expires_at=last_alert.created_at,
                score_delta=score_delta,
                ccu_spike_pct=ccu_spike,
            )

        if ccu_spike is not None and ccu_spike >= self.settings.ccu_spike_pct:
            return AlertDecision(
                level=level,
                should_notify=True,
                reason=SuppressionReason.CCU_SPIKE.value,
                score=score,
                cooldown_seconds=cooldown,
                cooldown_expires_at=last_alert.created_at,
                score_delta=score_delta,
                ccu_spike_pct=ccu_spike,
            )

        return AlertDecision(
            level=level,
            should_notify=False,
            reason=SuppressionReason.COOLDOWN.value,
            score=score,
            cooldown_seconds=cooldown,
            cooldown_expires_at=last_alert.created_at,
            score_delta=score_delta,
            ccu_spike_pct=ccu_spike,
        )

    # -------------------------------------------------------------- internal
    def _extreme_growth_override(self, growth: GrowthMetrics | None) -> float | None:
        """EXTREME on a very large *observed* CCU jump within a short window."""
        if growth is None or not growth.has_growth_data:
            return None
        for hours in (1, 6, 24):
            window = growth.window(hours)
            if window is None:
                continue
            if (
                window.pct_change >= self.settings.ccu_spike_pct * 2
                and window.absolute_change >= 500
            ):
                return window.pct_change
        return None

    def _ccu_spike(self, previous: int | None, current: int | None) -> float | None:
        if previous is None or current is None:
            return None
        from scoutforge.monitoring.growth import detect_spike

        return detect_spike(previous, current, threshold_pct=self.settings.ccu_spike_pct)


__all__ = [
    "AlertDecision",
    "AlertEngine",
    "LastAlertState",
    "SuppressionReason",
    "cooldown_for",
    "level_for_score",
]
