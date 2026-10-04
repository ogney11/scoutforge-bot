"""Hard filter gates.

The filters are the funnel's entry criteria, so every branch gets a test that
asserts both the verdict *and* the machine-readable rejection reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from scoutforge.config.settings import FilterSettings
from scoutforge.discovery.filters import GameFilter, RejectionReason
from scoutforge.discovery.models import CreatorInfo, GameDetails
from scoutforge.tests.conftest import NOW


def make_details(**overrides) -> GameDetails:
    """A game that passes every default gate; override one field per test."""
    base = {
        "universe_id": 900_000_001,
        "place_id": 1_234_567_890,
        "name": "Test Game",
        "creator": CreatorInfo(creator_id=42, creator_name="TestDev"),
        "current_ccu": 5_000,
        "lifetime_visits": 120_000,
    }
    base.update(overrides)
    return GameDetails(**base)


@pytest.fixture
def game_filter() -> GameFilter:
    return GameFilter(FilterSettings())


def reason_of(verdict) -> RejectionReason | None:
    return verdict.reason


class TestAccepts:
    def test_healthy_game_passes(self, game_filter: GameFilter) -> None:
        verdict = game_filter.evaluate(make_details(), now=NOW)
        assert verdict.passed is True
        assert verdict.reason is None
        assert bool(verdict) is True

    def test_exactly_at_min_ccu_passes(self) -> None:
        flt = GameFilter(FilterSettings(min_ccu=5_000))
        verdict = flt.evaluate(make_details(current_ccu=5_000), now=NOW)
        assert verdict.passed is True

    def test_just_under_max_visits_passes(self) -> None:
        flt = GameFilter(FilterSettings(max_lifetime_visits=120_000))
        assert flt.evaluate(make_details(lifetime_visits=119_999), now=NOW).passed is True

    def test_exactly_max_visits_is_rejected(self) -> None:
        """The gate is ``>=``, so the boundary itself is out of scope."""
        flt = GameFilter(FilterSettings(max_lifetime_visits=120_000))
        verdict = flt.evaluate(make_details(lifetime_visits=120_000), now=NOW)
        assert verdict.passed is False
        assert verdict.reason is RejectionReason.ABOVE_MAX_VISITS


class TestRejects:
    def test_missing_universe_id(self, game_filter: GameFilter) -> None:
        verdict = game_filter.evaluate(make_details(universe_id=0), now=NOW)
        assert verdict.passed is False
        assert reason_of(verdict) is RejectionReason.MISSING_UNIVERSE_ID

    def test_unidentifiable_game(self, game_filter: GameFilter) -> None:
        verdict = game_filter.evaluate(make_details(name=None, place_id=None), now=NOW)
        assert reason_of(verdict) is RejectionReason.UNIDENTIFIABLE

    def test_private_game(self, game_filter: GameFilter) -> None:
        assert reason_of(game_filter.evaluate(make_details(is_private=True), now=NOW)) == (
            RejectionReason.PRIVATE_GAME
        )

    def test_missing_ccu_is_rejected_not_guessed(self, game_filter: GameFilter) -> None:
        verdict = game_filter.evaluate(make_details(current_ccu=None), now=NOW)
        assert reason_of(verdict) is RejectionReason.MISSING_CCU
        assert any("cannot confirm" in w for w in verdict.warnings), verdict.warnings

    def test_non_positive_ccu(self, game_filter: GameFilter) -> None:
        assert reason_of(game_filter.evaluate(make_details(current_ccu=0), now=NOW)) == (
            RejectionReason.NON_POSITIVE_CCU
        )

    def test_below_min_ccu(self) -> None:
        flt = GameFilter(FilterSettings(min_ccu=1_000))
        verdict = flt.evaluate(make_details(current_ccu=999), now=NOW)
        assert reason_of(verdict) is RejectionReason.BELOW_MIN_CCU
        assert "999" in (verdict.detail or "")

    def test_missing_visits(self, game_filter: GameFilter) -> None:
        verdict = game_filter.evaluate(make_details(lifetime_visits=None), now=NOW)
        assert reason_of(verdict) is RejectionReason.MISSING_VISITS

    def test_at_or_above_max_visits(self) -> None:
        flt = GameFilter(FilterSettings(max_lifetime_visits=100_000))
        verdict = flt.evaluate(make_details(lifetime_visits=100_000), now=NOW)
        assert reason_of(verdict) is RejectionReason.ABOVE_MAX_VISITS

    def test_creator_required_but_absent(self) -> None:
        flt = GameFilter(FilterSettings(require_creator_id=True))
        verdict = flt.evaluate(make_details(creator=CreatorInfo()), now=NOW)
        assert reason_of(verdict) is RejectionReason.MISSING_CREATOR


class TestAbandoned:
    def _history(self, *, days: float, last_ccu: int) -> dict[str, object]:
        return {
            "first_seen_at": NOW - timedelta(days=days),
            "last_ccu": last_ccu,
        }

    def _abandoned_filter(self) -> GameFilter:
        return GameFilter(
            FilterSettings(min_ccu=0, abandoned_ccu_threshold=100, abandoned_after_days=14)
        )

    def test_collapsed_long_tracked_game_is_abandoned(self) -> None:
        flt = self._abandoned_filter()
        verdict = flt.evaluate(
            make_details(current_ccu=50),
            now=NOW,
            history=self._history(days=60, last_ccu=9_000),
        )
        assert reason_of(verdict) is RejectionReason.ABANDONED
        assert "9000" in (verdict.detail or "")

    def test_recently_tracked_game_is_not_abandoned(self) -> None:
        flt = self._abandoned_filter()
        verdict = flt.evaluate(
            make_details(current_ccu=50),
            now=NOW,
            history=self._history(days=2, last_ccu=9_000),
        )
        assert verdict.passed is True

    def test_still_busy_game_is_not_abandoned(self) -> None:
        flt = self._abandoned_filter()
        verdict = flt.evaluate(
            make_details(current_ccu=9_500),
            now=NOW,
            history=self._history(days=60, last_ccu=9_000),
        )
        assert verdict.passed is True

    def test_no_history_means_no_abandonment_verdict(self) -> None:
        flt = self._abandoned_filter()
        verdict = flt.evaluate(make_details(current_ccu=10), now=NOW, history={})
        assert verdict.passed is True, "games we have never seen cannot be called abandoned"

    def test_disabled_by_zero_settings(self) -> None:
        flt = GameFilter(
            FilterSettings(min_ccu=0, abandoned_ccu_threshold=0, abandoned_after_days=14)
        )
        verdict = flt.evaluate(
            make_details(current_ccu=1),
            now=NOW,
            history=self._history(days=999, last_ccu=50_000),
        )
        assert verdict.passed is True


class TestEvaluateMany:
    def test_keys_by_universe_id(self, game_filter: GameFilter) -> None:
        results = game_filter.evaluate_many(
            [
                make_details(universe_id=1, current_ccu=5_000),
                make_details(universe_id=2, current_ccu=5),
            ],
            now=NOW,
        )
        assert set(results) == {1, 2}
        assert results[1].passed is True
        assert results[2].passed is False


class TestRejectionReasonValues:
    def test_reasons_are_stable_strings(self) -> None:
        """These strings land in the DB and power /stats reporting."""
        assert RejectionReason.BELOW_MIN_CCU == "below_min_ccu"
        assert RejectionReason.ABOVE_MAX_VISITS == "above_max_lifetime_visits"
        assert RejectionReason.ABANDONED == "abandoned"


class TestClockInjection:
    def test_now_is_honoured(self) -> None:
        flt = GameFilter(
            FilterSettings(min_ccu=0, abandoned_ccu_threshold=100, abandoned_after_days=14)
        )
        past = NOW - timedelta(days=90)
        history = {"first_seen_at": past, "last_ccu": 5_000}
        # Same inputs, clock moved back inside the window -> not abandoned.
        assert (
            flt.evaluate(
                make_details(current_ccu=10), now=past + timedelta(days=1), history=history
            )
        ).passed
        assert not flt.evaluate(make_details(current_ccu=10), now=NOW, history=history).passed

    def test_naive_now_is_treated_as_utc(self, game_filter: GameFilter) -> None:
        naive = datetime(2026, 3, 1, 12, 0, 0)
        assert game_filter.evaluate(make_details(), now=naive).passed is True
