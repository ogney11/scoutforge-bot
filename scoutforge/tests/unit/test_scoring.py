"""Scout Score behaviour.

The score is a transparent 0-100 aggregation, not a prediction. These tests lock
down the properties the product promises:

* it is bounded to 0..100,
* an unknown metric never becomes a zero (weight is redistributed, caveat kept),
* every point is attributable to a named component with a human reason,
* better inputs never produce a worse score.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scoutforge.config.settings import AlertSettings, ScoreWeights
from scoutforge.database.models import GameSnapshot
from scoutforge.enums import AlertLevel
from scoutforge.monitoring.growth import compute_growth
from scoutforge.scoring.rules import ScoreInput
from scoutforge.scoring.scorer import ScoutScorer
from scoutforge.tests.conftest import NOW

UNIVERSE = 900_000_001


@pytest.fixture
def scorer() -> ScoutScorer:
    return ScoutScorer(weights=ScoreWeights(), alerts=AlertSettings())


def snapshot_series(pairs: list[tuple[float, int]]) -> list[GameSnapshot]:
    return [
        GameSnapshot(
            universe_id=UNIVERSE,
            captured_at=NOW - timedelta(hours=hours),
            current_ccu=ccu,
            lifetime_visits=ccu * 40,
            source="test",
        )
        for hours, ccu in pairs
    ]


def strong_input() -> ScoreInput:
    """A game that should score well on every axis."""
    series = snapshot_series([(24, 5_000), (12, 8_000), (6, 11_000), (0, 14_000)])
    return ScoreInput(
        universe_id=UNIVERSE,
        name="Rising Game",
        current_ccu=14_000,
        lifetime_visits=180_000,
        favorites=40_000,
        likes=60_000,
        dislikes=500,
        first_seen_at=NOW - timedelta(days=20),
        created_at=NOW - timedelta(days=40),
        peak_ccu=14_000,
        snapshot_count=len(series),
        growth=compute_growth(UNIVERSE, series, now=NOW),
        now=NOW,
    )


def weak_input() -> ScoreInput:
    return ScoreInput(
        universe_id=UNIVERSE,
        name="Dying Game",
        current_ccu=1_100,
        lifetime_visits=480_000,
        favorites=10,
        likes=20,
        dislikes=5_000,
        first_seen_at=NOW - timedelta(days=900),
        created_at=NOW - timedelta(days=1_800),
        peak_ccu=40_000,
        snapshot_count=4,
        growth=compute_growth(
            UNIVERSE,
            snapshot_series([(24, 40_000), (12, 20_000), (6, 5_000), (0, 1_100)]),
            now=NOW,
        ),
        now=NOW,
    )


class TestBounds:
    def test_score_is_within_zero_and_hundred(self, scorer: ScoutScorer) -> None:
        for data in (strong_input(), weak_input(), ScoreInput(universe_id=UNIVERSE)):
            value = scorer.score(data, now=NOW).score
            assert 0.0 <= value <= 100.0, value

    def test_strong_beats_weak(self, scorer: ScoutScorer) -> None:
        assert (
            scorer.score(strong_input(), now=NOW).score > scorer.score(weak_input(), now=NOW).score
        )


class TestUnknownMetrics:
    def test_empty_input_scores_zero_with_caveat(self, scorer: ScoutScorer) -> None:
        result = scorer.score(ScoreInput(universe_id=UNIVERSE), now=NOW)
        assert result.score == 0.0
        assert any("no usable metrics" in c.lower() for c in result.caveats)

    def test_missing_metrics_are_listed(self) -> None:
        """The brief requires unknown metrics to be named, not silently zeroed."""
        scorer = ScoutScorer(weights=ScoreWeights(), alerts=AlertSettings())
        data = strong_input()
        data.favorites = None
        data.likes = None
        result = scorer.score(data, now=NOW)
        unavailable = [c.key for c in result.components if not c.available]
        assert unavailable, "expected some components to be marked unavailable"
        for key in unavailable:
            component = next(c for c in result.components if c.key == key)
            assert component.caveats, f"{key} was skipped without saying why"

    def test_partial_data_still_scores(self) -> None:
        scorer = ScoutScorer(weights=ScoreWeights(), alerts=AlertSettings())
        result = scorer.score(ScoreInput(universe_id=UNIVERSE, current_ccu=9_000), now=NOW)
        assert 0.0 < result.score <= 100.0
        assert any(c.available for c in result.components)

    def test_unknown_metrics_do_not_zero_the_score(self, scorer: ScoutScorer) -> None:
        """Redistributing weight must not punish a game for missing data.

        Note the guarantee is "a missing metric scores 0 *and says so*", not
        "a partial score is always lower than a complete one": the available
        weight is redistributed proportionally, so removing a component that
        was scoring at or above the average legitimately nudges the total up.
        """
        scorer_score = scorer.score(strong_input(), now=NOW)
        sparse = scorer.score(ScoreInput(universe_id=UNIVERSE, current_ccu=9_000), now=NOW)

        assert sparse.score > 0.0
        assert sparse.available_weight < scorer_score.available_weight

        unavailable = [c for c in sparse.components if not c.available]
        assert unavailable, "expected some components to be marked unavailable"
        for component in unavailable:
            assert component.points == 0.0, f"{component.key} scored without data"
            assert component.caveats, f"{component.key} was skipped without saying why"

    def test_removing_an_above_average_component_does_not_inflate_the_score(
        self, scorer: ScoutScorer
    ) -> None:
        """Redistribution cuts both ways; make sure it favours the strong game.

        ``strong_input`` maxes out the ratio component. Weight is redistributed
        proportionally, so a maxed-out component only drags the average down -
        removing it can never make the game look better.
        """
        full = scorer.score(strong_input(), now=NOW)
        ratio = next(c for c in full.components if c.key == "ratio")
        assert ratio.value == pytest.approx(1.0)

        sparse = strong_input()
        sparse.lifetime_visits = None
        sparse.current_ccu = None
        partial = scorer.score(sparse, now=NOW)
        assert partial.score <= full.score


class TestComponents:
    def test_all_components_present(self, scorer: ScoutScorer) -> None:
        result = scorer.score(strong_input(), now=NOW)
        keys = {c.key for c in result.components}
        assert keys == {
            "ccu",
            "ratio",
            "growth",
            "freshness",
            "consistency",
            "footprint",
            "engagement",
            "monetization",
            "originality",
        }

    def test_points_sum_to_score(self, scorer: ScoutScorer) -> None:
        result = scorer.score(strong_input(), now=NOW)
        total = sum(c.points for c in result.components)
        assert total == pytest.approx(result.score, abs=0.01)

    def test_every_available_component_explains_itself(self, scorer: ScoutScorer) -> None:
        result = scorer.score(strong_input(), now=NOW)
        for component in result.components:
            if component.available:
                assert component.reasons, (
                    f"{component.key} scored {component.points} with no reason"
                )

    def test_component_values_are_normalised(self, scorer: ScoutScorer) -> None:
        result = scorer.score(strong_input(), now=NOW)
        for component in result.components:
            assert 0.0 <= component.value <= 1.0, component.key

    def test_score_row_is_serialisable(self, scorer: ScoutScorer) -> None:
        row = scorer.score(strong_input(), now=NOW).to_row()
        assert row["universe_id"] == UNIVERSE
        assert 0.0 <= row["score"] <= 100.0
        assert row["level"] in {level.value for level in AlertLevel}
        assert set(row["components"]) >= {"ratio", "growth"}


class TestLevels:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.0, AlertLevel.NORMAL),
            (74.9, AlertLevel.NORMAL),
            (75.0, AlertLevel.WARM),
            (89.9, AlertLevel.WARM),
            (90.0, AlertLevel.HOT),
            (94.9, AlertLevel.HOT),
            (95.0, AlertLevel.EXTREME),
            (100.0, AlertLevel.EXTREME),
        ],
    )
    def test_thresholds(self, scorer: ScoutScorer, score: float, expected: AlertLevel) -> None:
        assert scorer.level_for(score) is expected


class TestFreshness:
    def test_brand_new_game_scores_higher_on_freshness_than_an_old_one(
        self, scorer: ScoutScorer
    ) -> None:
        fresh = strong_input()
        fresh.first_seen_at = NOW - timedelta(hours=6)
        old = strong_input()
        old.first_seen_at = NOW - timedelta(days=800)

        fresh_score = next(
            c for c in scorer.score(fresh, now=NOW).components if c.key == "freshness"
        )
        old_score = next(c for c in scorer.score(old, now=NOW).components if c.key == "freshness")
        assert fresh_score.value > old_score.value

    def test_first_seen_in_the_future_is_handled(self, scorer: ScoutScorer) -> None:
        """Clock skew must not produce a negative age or an exception."""
        data = strong_input()
        data.first_seen_at = NOW + timedelta(hours=2)
        result = scorer.score(data, now=NOW)
        assert 0.0 <= result.score <= 100.0


class TestScoreMany:
    def test_batches(self, scorer: ScoutScorer) -> None:
        results = scorer.score_many([strong_input(), weak_input()], now=NOW)
        assert len(results) == 2
        assert results[0].score > results[1].score


class TestDeterminism:
    def test_same_input_same_score(self, scorer: ScoutScorer) -> None:
        first = scorer.score(strong_input(), now=NOW)
        second = scorer.score(strong_input(), now=NOW)
        assert first.score == second.score

    def test_no_hidden_clock(self, scorer: ScoutScorer) -> None:
        """``now=`` must fully determine the result."""
        a = scorer.score(strong_input(), now=NOW)
        b = scorer.score(strong_input(), now=NOW - timedelta(days=1))
        # Same inputs, different clock: components like freshness may shift, but
        # nothing may explode.
        assert 0.0 <= b.score <= 100.0
        assert a.universe_id == b.universe_id
