"""Runtime configuration overrides stored in the database.

``/alerts threshold <score>`` writes here; the alert engine reads it before each
evaluation, so a scout can retune the bot without a restart or redeploy.

Only an explicit allow-list of keys may be changed from Discord, and each value
is validated against a declared type and range.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import AlertSettings
from scoutforge.database.repositories import SettingsRepository
from scoutforge.enums import AlertLevel

log = get_logger(__name__)

KEY_ALERTS_ENABLED = "alerts.enabled"
KEY_HOT_SCORE = "alerts.hot_score"
KEY_EXTREME_SCORE = "alerts.extreme_score"
KEY_WARM_SCORE = "alerts.warm_score"
KEY_HOT_COOLDOWN = "alerts.hot_cooldown_seconds"
KEY_EXTREME_COOLDOWN = "alerts.extreme_cooldown_seconds"
KEY_LEAD_MIN_SCORE = "alerts.lead_min_score"


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Declarative description of a runtime-tunable setting."""

    key: str
    kind: type
    description: str
    minimum: float | None = None
    maximum: float | None = None

    def validate(self, value: Any) -> Any:
        if isinstance(value, bool) and self.kind is not bool:
            raise ValueError(f"{self.key} must be {self.kind.__name__}")
        try:
            coerced = self.kind(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.key} must be {self.kind.__name__}") from exc
        if self.minimum is not None and coerced < self.minimum:
            raise ValueError(f"{self.key} must be >= {self.minimum}")
        if self.maximum is not None and coerced > self.maximum:
            raise ValueError(f"{self.key} must be <= {self.maximum}")
        return coerced


SETTINGS: dict[str, SettingSpec] = {
    KEY_ALERTS_ENABLED: SettingSpec(
        KEY_ALERTS_ENABLED, bool, "Master switch for HOT/EXTREME Discord alerts"
    ),
    KEY_WARM_SCORE: SettingSpec(KEY_WARM_SCORE, float, "Minimum score for WARM", 0, 100),
    KEY_HOT_SCORE: SettingSpec(KEY_HOT_SCORE, float, "Minimum score for HOT", 0, 100),
    KEY_EXTREME_SCORE: SettingSpec(KEY_EXTREME_SCORE, float, "Minimum score for EXTREME", 0, 100),
    KEY_HOT_COOLDOWN: SettingSpec(
        KEY_HOT_COOLDOWN, int, "HOT re-alert cooldown in seconds", 0, 604_800
    ),
    KEY_EXTREME_COOLDOWN: SettingSpec(
        KEY_EXTREME_COOLDOWN, int, "EXTREME re-alert cooldown in seconds", 0, 604_800
    ),
    KEY_LEAD_MIN_SCORE: SettingSpec(
        KEY_LEAD_MIN_SCORE, float, "Only create leads at or above this score", 0, 100
    ),
}


class UnknownSettingError(ValueError):
    """Raised when a caller touches a setting that is not allow-listed."""


class SettingsService:
    """Reads/writes runtime overrides and applies them to the alert settings."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = SettingsRepository(session)

    async def get(self, key: str) -> Any:
        return await self.repository.get(key)

    async def set(self, key: str, value: Any, *, updated_by: int | None = None) -> Any:
        spec = SETTINGS.get(key)
        if spec is None:
            raise UnknownSettingError(f"{key} is not a runtime-adjustable setting")
        coerced = spec.validate(value)
        await self.repository.set(key, coerced, description=spec.description, updated_by=updated_by)
        log_event(log, 20, "SETTING_CHANGED", key=key, value=coerced, by=updated_by)
        return coerced

    async def all(self) -> dict[str, Any]:
        values = await self.repository.all()
        return {key: values[key] for key in SETTINGS if key in values}

    async def reset(self, key: str, *, updated_by: int | None = None) -> None:
        from sqlalchemy import delete

        from scoutforge.database.models import Setting

        if key not in SETTINGS:
            raise UnknownSettingError(f"{key} is not a runtime-adjustable setting")
        await self.session.execute(delete(Setting).where(Setting.key == key))
        await self.session.flush()
        log_event(log, 20, "SETTING_RESET", key=key, by=updated_by)

    async def effective_alerts(
        self, base: AlertSettings | None = None, *, fallbacks: Callable[[str], Any] | None = None
    ) -> AlertSettings:
        """Return alert settings with database overrides applied.

        Args:
            base: Environment settings to start from.
            fallbacks: Not used by default; reserved for layered configuration.
        """
        del fallbacks
        base = base or AlertSettings()
        overrides = await self.all()
        if not overrides:
            return base

        updates: dict[str, Any] = {}
        if KEY_ALERTS_ENABLED in overrides:
            updates["enabled"] = bool(overrides[KEY_ALERTS_ENABLED])
        if KEY_WARM_SCORE in overrides:
            updates["warm_score"] = float(overrides[KEY_WARM_SCORE])
        if KEY_HOT_SCORE in overrides:
            updates["hot_score"] = float(overrides[KEY_HOT_SCORE])
        if KEY_EXTREME_SCORE in overrides:
            updates["extreme_score"] = float(overrides[KEY_EXTREME_SCORE])
        if KEY_HOT_COOLDOWN in overrides:
            updates["hot_cooldown_seconds"] = int(overrides[KEY_HOT_COOLDOWN])
        if KEY_EXTREME_COOLDOWN in overrides:
            updates["extreme_cooldown_seconds"] = int(overrides[KEY_EXTREME_COOLDOWN])
        if KEY_LEAD_MIN_SCORE in overrides:
            updates["lead_min_score"] = float(overrides[KEY_LEAD_MIN_SCORE])

        try:
            return base.model_copy(update=updates)
        except Exception as exc:
            log_event(log, 30, "SETTING_OVERRIDE_INVALID", error=type(exc).__name__)
            return base

    async def alert_level_for(self, score: float, base: AlertSettings | None = None) -> AlertLevel:
        settings = await self.effective_alerts(base)
        if score >= settings.extreme_score:
            return AlertLevel.EXTREME
        if score >= settings.hot_score:
            return AlertLevel.HOT
        if score >= settings.warm_score:
            return AlertLevel.WARM
        return AlertLevel.NORMAL

    async def describe(self) -> list[tuple[str, Any, str]]:
        """``(key, value, description)`` for every runtime-adjustable setting."""
        stored = await self.all()
        return [
            (spec.key, stored.get(spec.key, "(env default)"), spec.description)
            for spec in SETTINGS.values()
        ]


__all__ = [
    "KEY_ALERTS_ENABLED",
    "KEY_EXTREME_COOLDOWN",
    "KEY_EXTREME_SCORE",
    "KEY_HOT_COOLDOWN",
    "KEY_HOT_SCORE",
    "KEY_LEAD_MIN_SCORE",
    "KEY_WARM_SCORE",
    "SETTINGS",
    "SettingSpec",
    "SettingsService",
    "UnknownSettingError",
]
