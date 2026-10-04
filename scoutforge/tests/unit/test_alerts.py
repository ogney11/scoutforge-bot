"""Alert level classification, cooldown suppression and bypass overrides.

Alerting is the part of ScoutForge that talks to humans, so the tests are
strict about three things: never notify below WARM, never spam inside a
cooldown, and always let a genuine escalation or spike through.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scoutforge.config.settings import AlertSettings
from scoutforge.database.models import GameSnapshot
from scoutforge.enums import AlertLevel
from scoutforge.monitoring.alerts import (
    AlertEngine,
    LastAlertState,
    SuppressionReason,
    cooldown_for,
    level_for_score,
)
from scoutforge.monitoring.growth import compute_growth
from scoutforge.tests.conftest import NOW

UNIVERSE = 900_000_001


@pytest.fixture
def settings() -> AlertSettings:
    return AlertSettings()


@pytest.fixture
def engine(settings: AlertSettings) -> AlertEngine:
    return AlertEngine(settings)


def growth_from(pairs: list[tuple[float, int]]):
    series = [
        GameSnapshot(
            universe_id=UNIVERSE,
            captured_at=NOW - timedelta(hours=hours),
            current_ccu=ccu,
            source="test",
        )
        for hours, ccu in pairs
    ]
    return compute_growth(UNIVERSE, series, now=NOW)


class TestLevelMapping:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.0, AlertLevel.NORMAL),
            (74.99, AlertLevel.NORMAL),
            (75.0, AlertLevel.WARM),
            (89.99, AlertLevel.WARM),
            (90.0, AlertLevel.HOT),
            (94.99, AlertLevel.HOT),
            (95.0, AlertLevel.EXTREME),
        ],
    )
    def test_thresholds(self, settings: AlertSettings, score: float, expected: AlertLevel) -> None:
        assert level_for_score(score, settings) is expected

    def test_extreme_has_the_shortest_cooldown(self, settings: AlertSettings) -> None:
        assert (
            cooldown_for(AlertLevel.EXTREME, settings)
            < cooldown_for(AlertLevel.HOT, settings)
            < cooldown_for(AlertLevel.WARM, settings)
        )

    def test_normal_level_uses_the_warm_cooldown(self, settings: AlertSettings) -> None:
        assert cooldown_for(AlertLevel.NORMAL, settings) == settings.warm_cooldown_seconds


class TestGating:
    def test_normal_level_never_notifies(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(score=10.0, now=NOW)
        assert decision.should_notify is False
        assert decision.reason == SuppressionReason.BELOW_WARM_THRESHOLD.value

    def test_warm_level_does_not_notify(self, engine: AlertEngine) -> None:
        """WARM is for reporting, not for pinging Discord."""
        decision = engine.evaluate(score=80.0, now=NOW)
        assert decision.level is AlertLevel.WARM
        assert decision.should_notify is False

    def test_hot_level_notifies_on_first_sight(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(score=92.0, now=NOW)
        assert decision.should_notify is True
        assert decision.reason == "first_alert"
        assert decision.cooldown_seconds > 0

    def test_disabled_alerts_suppress_everything(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(score=99.0, now=NOW, alerts_enabled=False)
        assert decision.should_notify is False
        assert decision.reason == SuppressionReason.DISABLED.value

    def test_explicit_enabled_overrides_settings(self, settings: AlertSettings) -> None:
        engine = AlertEngine(AlertSettings(enabled=False))
        assert engine.evaluate(score=99.0, now=NOW, alerts_enabled=True).should_notify is True


class TestCooldown:
    def _last(self, *, level: AlertLevel, score: float, age_seconds: float, ccu: int | None = None):
        return LastAlertState(
            created_at=NOW - timedelta(seconds=age_seconds),
            level=level,
            score=score,
            ccu=ccu,
        )

    def test_repeat_inside_cooldown_is_suppressed(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=92.0,
            last_alert=self._last(level=AlertLevel.HOT, score=91.0, age_seconds=60),
            now=NOW,
        )
        assert decision.should_notify is False
        assert decision.reason == SuppressionReason.COOLDOWN.value
        assert decision.cooldown_expires_at is not None

    def test_repeat_after_cooldown_expires_notifies(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=92.0,
            last_alert=self._last(level=AlertLevel.HOT, score=91.0, age_seconds=21_601),
            now=NOW,
        )
        assert decision.should_notify is True
        assert decision.reason == "cooldown_expired"

    def test_zero_cooldown_always_notifies(self, settings: AlertSettings) -> None:
        engine = AlertEngine(AlertSettings(hot_cooldown_seconds=0))
        decision = engine.evaluate(
            score=92.0,
            last_alert=self._last(level=AlertLevel.HOT, score=92.0, age_seconds=1),
            now=NOW,
        )
        assert decision.should_notify is True


class TestOverrides:
    def _last(self, *, level: AlertLevel, score: float, ccu: int | None = None):
        return LastAlertState(
            created_at=NOW - timedelta(seconds=60), level=level, score=score, ccu=ccu
        )

    def test_score_jump_bypasses_cooldown(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=99.0,
            last_alert=self._last(level=AlertLevel.EXTREME, score=93.0),
            now=NOW,
        )
        assert decision.should_notify is True
        assert decision.reason == SuppressionReason.SCORE_JUMP.value
        assert decision.is_override is True

    def test_small_score_move_stays_suppressed(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=92.0,
            last_alert=self._last(level=AlertLevel.HOT, score=91.0),
            now=NOW,
        )
        assert decision.should_notify is False

    def test_escalation_is_reported_before_the_score_jump(self, engine: AlertEngine) -> None:
        """Both bypass the cooldown; the stronger signal is reported."""
        decision = engine.evaluate(
            score=99.0,
            last_alert=self._last(level=AlertLevel.HOT, score=90.0),
            now=NOW,
        )
        assert decision.reason == "level_escalated"

    def test_ccu_spike_bypasses_cooldown(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=92.0,
            current_ccu=10_000,
            last_alert=self._last(level=AlertLevel.HOT, score=92.0, ccu=1_000),
            now=NOW,
        )
        assert decision.should_notify is True
        assert decision.reason == SuppressionReason.CCU_SPIKE.value
        assert decision.ccu_spike_pct == pytest.approx(900.0)

    def test_ccu_decline_stays_suppressed(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=92.0,
            current_ccu=500,
            last_alert=self._last(level=AlertLevel.HOT, score=92.0, ccu=10_000),
            now=NOW,
        )
        assert decision.should_notify is False

    def test_level_escalation_bypasses_cooldown(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(
            score=96.0,
            last_alert=self._last(level=AlertLevel.HOT, score=91.0),
            now=NOW,
        )
        assert decision.should_notify is True
        assert decision.level is AlertLevel.EXTREME
        assert decision.is_override is True

    def test_de_escalation_stays_suppressed(self, engine: AlertEngine) -> None:
        """EXTREME then HOT is a downgrade; the cooldown still applies."""
        decision = engine.evaluate(
            score=91.0,
            last_alert=self._last(level=AlertLevel.EXTREME, score=96.0),
            now=NOW,
        )
        assert decision.should_notify is False


class TestExtremeByGrowth:
    """The growth override must rest on *observed* data, not assumptions."""

    def test_huge_measured_jump_promotes_to_extreme(self, engine: AlertEngine) -> None:
        growth = growth_from([(24, 1_000), (12, 1_200), (6, 1_500), (1, 2_000), (0, 6_000)])
        decision = engine.evaluate(score=80.0, growth=growth, now=NOW)
        assert decision.level is AlertLevel.EXTREME
        assert decision.should_notify is True

    def test_percentage_without_absolute_jump_is_not_enough(self, engine: AlertEngine) -> None:
        """+200% but only +60 players must not page anyone."""
        growth = growth_from([(24, 30), (0, 90)])
        decision = engine.evaluate(score=80.0, growth=growth, now=NOW)
        assert decision.level is AlertLevel.WARM
        assert decision.should_notify is False

    def test_absolute_jump_without_percentage_is_not_enough(self, engine: AlertEngine) -> None:
        """+4000 players but +50% must not page anyone either."""
        growth = growth_from([(24, 8_000), (0, 12_000)])
        decision = engine.evaluate(score=80.0, growth=growth, now=NOW)
        assert decision.level is AlertLevel.WARM

    def test_no_growth_data_means_no_override(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(score=80.0, growth=None, now=NOW)
        assert decision.level is AlertLevel.WARM
        assert decision.should_notify is False

    def test_flat_history_means_no_override(self, engine: AlertEngine) -> None:
        growth = growth_from([(24, 5_000), (12, 5_000), (6, 5_000), (0, 5_000)])
        decision = engine.evaluate(score=80.0, growth=growth, now=NOW)
        assert decision.level is AlertLevel.WARM


class TestDecisionShape:
    def test_decision_records_the_score(self, engine: AlertEngine) -> None:
        assert engine.evaluate(score=91.5, now=NOW).score == 91.5

    def test_non_override_reason_is_not_flagged_as_override(self, engine: AlertEngine) -> None:
        assert engine.evaluate(score=92.0, now=NOW).is_override is False

    def test_decision_is_inert_without_alerts_enabled(self, engine: AlertEngine) -> None:
        decision = engine.evaluate(score=99.0, now=NOW, alerts_enabled=False)
        assert decision.cooldown_seconds == 0
        assert decision.score == 99.0
