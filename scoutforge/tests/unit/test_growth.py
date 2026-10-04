"""Growth, stability and spike detection.

These functions are the mathematical heart of the product: every score and every
alert traces back to them. The tests pin the arithmetic to hand-computed values
so a refactor cannot quietly change what "200% growth" means.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from scoutforge.database.models import GameSnapshot
from scoutforge.monitoring.growth import _stability, compute_growth, detect_spike
from scoutforge.tests.conftest import NOW

UNIVERSE = 900_000_001


def snapshots(points: list[tuple[float, int | None]], *, universe_id: int = UNIVERSE):
    """Build snapshots from ``(hours_ago, ccu)`` pairs, oldest first."""
    return [
        GameSnapshot(
            universe_id=universe_id,
            captured_at=NOW - timedelta(hours=hours),
            current_ccu=ccu,
            lifetime_visits=(ccu or 0) * 40 if ccu is not None else None,
            source="test",
        )
        for hours, ccu in points
    ]


class TestComputeGrowthWindows:
    def test_doubling_over_24h_is_100_percent(self) -> None:
        """1000 -> 2000 over exactly 24h must read as +100%."""
        series = snapshots([(24, 1_000), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        window = metrics.windows[24]
        assert window is not None
        assert window.from_ccu == 1_000
        assert window.to_ccu == 2_000
        assert window.pct_change == pytest.approx(100.0)
        assert metrics.growth_24h_pct == pytest.approx(100.0)

    def test_halving_is_negative_100_percent(self) -> None:
        series = snapshots([(24, 4_000), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.growth_24h_pct == pytest.approx(-50.0)

    def test_multiple_windows_from_one_series(self) -> None:
        series = snapshots([(24, 1_000), (6, 1_500), (1, 1_800), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert set(metrics.windows) == {1, 6, 24}
        assert metrics.growth_1h_pct == pytest.approx((2_000 - 1_800) / 1_800 * 100)
        assert metrics.growth_6h_pct == pytest.approx((2_000 - 1_500) / 1_500 * 100)

    def test_peak_ccu_is_the_maximum_observed(self) -> None:
        series = snapshots([(6, 5_000), (3, 9_000), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.peak_ccu == 9_000

    def test_ccus_per_day_is_a_24h_normalised_rate(self) -> None:
        """+2000 CCU across 48h is +1000/day, not +2000."""
        series = snapshots([(48, 1_000), (24, 1_500), (0, 3_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.ccus_per_day == pytest.approx(1_000.0)

    def test_snapshots_are_sorted_regardless_of_input_order(self) -> None:
        series = snapshots([(0, 2_000), (24, 1_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.growth_24h_pct == pytest.approx(100.0)


class TestComputeGrowthHonesty:
    """Missing data must degrade to a caveat, never to a fabricated number."""

    def test_no_snapshots(self) -> None:
        metrics = compute_growth(UNIVERSE, [], now=NOW)
        assert metrics.has_growth_data is False
        assert metrics.windows == {}
        assert metrics.growth_24h_pct is None
        assert metrics.caveats

    def test_single_snapshot_is_not_growth(self) -> None:
        metrics = compute_growth(UNIVERSE, snapshots([(0, 1_000)]), now=NOW)
        assert metrics.has_growth_data is False
        assert "one observation" in " ".join(metrics.caveats).lower()

    def test_short_history_reports_no_24h_window(self) -> None:
        series = snapshots([(3, 1_000), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert 24 not in metrics.windows
        assert metrics.growth_24h_pct is None
        # The operator must be told *why* the 24h figure is absent.
        assert any("24h" in c for c in metrics.caveats), metrics.caveats

    def test_null_ccu_rows_are_skipped(self) -> None:
        series = snapshots([(24, 1_000), (12, None), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.growth_24h_pct == pytest.approx(100.0)
        assert metrics.peak_ccu == 2_000

    def test_all_null_ccu_yields_no_metrics(self) -> None:
        metrics = compute_growth(UNIVERSE, snapshots([(2, None), (0, None)]), now=NOW)
        assert metrics.has_growth_data is False

    def test_window_under_one_hour_has_no_velocity(self) -> None:
        series = snapshots([(0.2, 1_000), (0, 2_000)])
        metrics = compute_growth(UNIVERSE, series, now=NOW)
        assert metrics.ccus_per_day is None
        assert any("under 1h" in c for c in metrics.caveats)


class TestStability:
    def test_steady_climb_is_maximally_stable(self) -> None:
        ccus = [5_000, 6_000, 7_000, 8_000, 9_000, 10_000, 11_000, 12_000, 13_000]
        assert _stability(ccus) == pytest.approx(1.0, abs=0.01)

    def test_oscillation_scores_zero(self) -> None:
        ccus = [1_000, 20_000, 1_000, 20_000, 1_000, 20_000]
        assert _stability(ccus) == pytest.approx(0.0, abs=0.01)

    def test_flat_series_is_stable(self) -> None:
        assert _stability([1_000] * 8) == pytest.approx(1.0, abs=0.01)

    def test_stability_is_bounded(self) -> None:
        for ccus in ([0, 0, 0], [100, 1, 100], list(range(0, 200, 7))):
            value = _stability(ccus)
            assert 0.0 <= value <= 1.0

    def test_growth_does_not_reduce_stability(self) -> None:
        """A steady climb must not be penalised as 'erratic'."""
        rising = _stability([5_000, 6_000, 7_000, 8_000, 9_000])
        flat = _stability([7_000, 7_000, 7_000, 7_000, 7_000])
        assert rising >= flat - 0.05

    def test_consistency_needs_three_samples(self) -> None:
        metrics = compute_growth(UNIVERSE, snapshots([(2, 100), (1, 200), (0, 300)]), now=NOW)
        assert metrics.has_consistency_data is True
        assert metrics.stability is not None


class TestDetectSpike:
    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            (1_000, 1_500, 50.0),
            (1_000, 2_000, 100.0),
            (1_000, 10_000, 900.0),
        ],
    )
    def test_percentage_above_threshold(self, previous: int, current: int, expected: float) -> None:
        assert detect_spike(previous, current, threshold_pct=50.0) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("previous", "current"),
        [
            (1_000, 1_100),  # +10%
            (1_000, 500),  # -50%: a drop is not a spike
            (1_000, 1_000),  # flat
        ],
    )
    def test_no_spike_at_or_below_threshold(self, previous: int, current: int) -> None:
        """The rule is directional: only growth counts as a spike."""
        assert detect_spike(previous, current, threshold_pct=50.0) is None

    def test_just_below_threshold_is_not_a_spike(self) -> None:
        assert detect_spike(1_000, 1_400, threshold_pct=50.0) is None

    def test_exactly_at_threshold_is_a_spike(self) -> None:
        assert detect_spike(1_000, 1_500, threshold_pct=50.0) == pytest.approx(50.0)

    def test_missing_baseline_is_not_a_spike(self) -> None:
        assert detect_spike(None, 5_000, threshold_pct=50.0) is None

    def test_zero_baseline_is_not_a_spike(self) -> None:
        """Growth from zero is undefined, not infinite."""
        assert detect_spike(0, 5_000, threshold_pct=50.0) is None

    def test_missing_current_is_not_a_spike(self) -> None:
        assert detect_spike(1_000, None, threshold_pct=50.0) is None


class TestSerialisation:
    def test_to_dict_keeps_none_as_none(self) -> None:
        metrics = compute_growth(UNIVERSE, [], now=NOW)
        payload = metrics.to_dict()
        assert payload["stability"] is None
        assert payload["ccus_per_day"] is None
        assert payload["windows"] == {}
        assert payload["caveats"]

    def test_to_dict_reports_windows(self) -> None:
        metrics = compute_growth(UNIVERSE, snapshots([(24, 1_000), (0, 2_000)]), now=NOW)
        payload = metrics.to_dict()
        assert payload["windows"]["24"]["pct_change"] == pytest.approx(100.0)
        assert payload["windows"]["24"]["from_ccu"] == 1_000
        assert payload["windows"]["24"]["to_ccu"] == 2_000
