"""Alert orchestration: decide, persist, then hand off to a dispatcher.

The dispatcher is an interface, which keeps the worker fully testable (a fake
dispatcher captures payloads) and keeps Discord concerns out of the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import AlertSettings
from scoutforge.database.models import Game
from scoutforge.database.repositories import AlertRepository, StatsRepository
from scoutforge.enums import AlertLevel
from scoutforge.monitoring.alerts import AlertDecision, AlertEngine, LastAlertState
from scoutforge.monitoring.growth import GrowthMetrics
from scoutforge.scoring.scorer import ScoutScore
from scoutforge.utils import utcnow

log = get_logger(__name__)


@runtime_checkable
class AlertDispatcher(Protocol):
    """Delivers a rendered alert payload to a human channel."""

    async def send_alert(self, payload: dict[str, Any]) -> int | None:
        """Deliver the alert and return the platform message id, if any."""
        ...


@dataclass(slots=True)
class AlertOutcome:
    """Result of evaluating one game for alerting."""

    universe_id: int
    decision: AlertDecision
    alert_id: int | None = None
    delivered: bool = False
    message_id: int | None = None
    error: str | None = None

    @property
    def level(self) -> AlertLevel:
        return self.decision.level

    @property
    def notified(self) -> bool:
        return self.delivered


@dataclass(slots=True)
class AlertPayload:
    """Rendered alert content handed to the dispatcher."""

    universe_id: int
    game_name: str
    level: AlertLevel
    score: float
    current_ccu: int | None
    lifetime_visits: int | None
    ccu_to_visits_ratio: float | None
    growth_pct: float | None
    growth_window_hours: int | None
    roblox_url: str
    discord_url: str | None
    creator_name: str | None
    creator_type: str | None
    thumbnail_url: str | None
    first_seen_at: datetime | None
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    partners: list[dict[str, Any]] = field(default_factory=list)
    mention: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_id,
            "game_name": self.game_name,
            "level": self.level.value,
            "score": round(self.score, 2),
            "current_ccu": self.current_ccu,
            "lifetime_visits": self.lifetime_visits,
            "ccu_to_visits_ratio": (
                round(self.ccu_to_visits_ratio, 6) if self.ccu_to_visits_ratio is not None else None
            ),
            "growth_pct": round(self.growth_pct, 2) if self.growth_pct is not None else None,
            "growth_window_hours": self.growth_window_hours,
            "roblox_url": self.roblox_url,
            "discord_url": self.discord_url,
            "creator_name": self.creator_name,
            "creator_type": self.creator_type,
            "thumbnail_url": self.thumbnail_url,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "reasons": list(self.reasons),
            "caveats": list(self.caveats),
            "partners": [dict(partner) for partner in self.partners],
            "mention": self.mention,
        }


class AlertService:
    """Applies the cooldown rules and records alert history."""

    def __init__(
        self,
        session: AsyncSession,
        engine: AlertEngine | None = None,
        *,
        dispatcher: AlertDispatcher | None = None,
        settings: AlertSettings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or AlertSettings()
        self.engine = engine or AlertEngine(self.settings)
        self.dispatcher = dispatcher
        self.repository = AlertRepository(session)
        self.stats = StatsRepository(session)

    async def last_delivered_state(self, universe_id: int) -> LastAlertState | None:
        """Load the most recent delivered alert, used for cooldown checks."""
        alert = await self.repository.last_any_delivered(universe_id)
        if alert is None:
            return None
        return LastAlertState(
            created_at=alert.created_at,
            level=AlertLevel(alert.level),
            score=alert.score,
            ccu=alert.ccu,
        )

    async def process(
        self,
        *,
        game: Game,
        score: ScoutScore,
        growth: GrowthMetrics | None = None,
        partners: list[dict[str, Any]] | None = None,
        alerts_enabled: bool | None = None,
        now: datetime | None = None,
    ) -> AlertOutcome:
        """Evaluate, persist and (when warranted) deliver an alert.

        ``partners`` carries the matcher's eligible matches so the Discord embed
        can say who this game is for without a second lookup.
        """
        now = now or utcnow()
        last_state = await self.last_delivered_state(game.universe_id)

        decision = self.engine.evaluate(
            score=score.score,
            growth=growth,
            last_alert=last_state,
            current_ccu=game.current_ccu,
            now=now,
            alerts_enabled=alerts_enabled,
        )

        if decision.should_notify:
            payload = self.build_payload(game, score, growth, decision, partners=partners)
            alert_row = await self.repository.add(
                {
                    "universe_id": game.universe_id,
                    "level": decision.level.value,
                    "score": score.score,
                    "previous_score": last_state.score if last_state else None,
                    "score_delta": decision.score_delta,
                    "ccu": game.current_ccu,
                    "ccu_spike_pct": decision.ccu_spike_pct,
                    "delivered": False,
                    "suppressed_reason": None,
                    "created_at": now,
                }
            )
            message_id = None
            error = None
            if self.dispatcher is not None:
                try:
                    message_id = await self.dispatcher.send_alert(payload.to_dict())
                    alert_row.delivered = True
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:500]
                    alert_row.error = error
                    log_event(
                        log,
                        40,
                        "ALERT_DELIVERY_FAILED",
                        universe=game.universe_id,
                        alert_level=decision.level.value,
                        error=type(exc).__name__,
                    )
            else:
                # No dispatcher (tests / dashboard-only mode): record, do not claim delivery.
                alert_row.delivered = False
                alert_row.suppressed_reason = "no_dispatcher"

            if alert_row.delivered:
                await self.stats.increment("alerts_sent")
                await self.stats.increment(f"alerts_{decision.level.value.lower()}")
            else:
                await self.stats.increment("alerts_failed")
            log_event(
                log,
                20,
                "ALERT",
                universe=game.universe_id,
                alert_level=decision.level.value,
                score=score.score,
                delivered=alert_row.delivered,
                reason=decision.reason,
            )
            return AlertOutcome(
                universe_id=game.universe_id,
                decision=decision,
                alert_id=alert_row.id,
                delivered=bool(alert_row.delivered),
                message_id=message_id,
                error=error,
            )

        alert_row = await self.repository.add(
            {
                "universe_id": game.universe_id,
                "level": decision.level.value,
                "score": score.score,
                "previous_score": last_state.score if last_state else None,
                "score_delta": decision.score_delta,
                "ccu": game.current_ccu,
                "ccu_spike_pct": decision.ccu_spike_pct,
                "delivered": False,
                "suppressed_reason": decision.reason,
                "created_at": now,
            }
        )
        if decision.level.rank >= AlertLevel.HOT.rank:
            await self.stats.increment("alerts_suppressed")
        log_event(
            log,
            20,
            "ALERT",
            universe=game.universe_id,
            alert_level=decision.level.value,
            score=score.score,
            delivered=False,
            reason=decision.reason,
        )
        return AlertOutcome(universe_id=game.universe_id, decision=decision, alert_id=alert_row.id)

    # -------------------------------------------------------------- payload
    def build_payload(
        self,
        game: Game,
        score: ScoutScore,
        growth: GrowthMetrics | None,
        decision: AlertDecision,
        *,
        partners: list[dict[str, Any]] | None = None,
    ) -> AlertPayload:
        window = growth.primary_growth if growth else None
        return AlertPayload(
            universe_id=game.universe_id,
            game_name=game.game_name or f"Universe {game.universe_id}",
            level=decision.level,
            score=score.score,
            current_ccu=game.current_ccu,
            lifetime_visits=game.lifetime_visits,
            ccu_to_visits_ratio=game.ccu_to_visits_ratio,
            growth_pct=window.pct_change if window else None,
            growth_window_hours=window.window_hours if window else None,
            roblox_url=game.public_url,
            discord_url=game.public_discord_url,
            creator_name=game.creator_name,
            creator_type=game.creator_type,
            thumbnail_url=game.thumbnail_url,
            first_seen_at=game.first_seen_at,
            reasons=score.reasons,
            caveats=score.caveats,
            partners=list(partners or []),
        )


__all__ = [
    "AlertDispatcher",
    "AlertOutcome",
    "AlertPayload",
    "AlertService",
]
