"""Unit tests for the shared helpers in :mod:`scoutforge.utils`.

These functions decide what the dashboard and embeds show, so the important
property is that unknown data degrades to a visible placeholder instead of a
crash, a ``0``, or a misleading number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from scoutforge.tests.conftest import NOW
from scoutforge.utils import (
    as_utc,
    day_bounds,
    ensure_utc,
    format_compact,
    format_duration,
    format_pct,
    hours_since,
    isoformat,
    parse_roblox_timestamp,
    percent_change,
    relative_time,
    safe_div,
    seconds_since,
    utcnow,
)


class TestTimeNormalisation:
    def test_utcnow_is_aware(self) -> None:
        assert utcnow().tzinfo is UTC

    def test_ensure_utc_treats_naive_as_utc(self) -> None:
        naive = datetime(2026, 3, 1, 12, 0, 0)
        assert ensure_utc(naive) == datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

    def test_ensure_utc_converts_other_zones(self) -> None:
        other = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
        assert ensure_utc(other) == datetime(2026, 3, 1, 7, 0, 0, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None])
    def test_none_stays_none(self, value: None) -> None:
        assert ensure_utc(value) is None
        assert as_utc(value) is None

    def test_as_utc_is_an_alias(self) -> None:
        naive = datetime(2026, 3, 1, 12, 0, 0)
        assert as_utc(naive) == ensure_utc(naive)


class TestElapsed:
    def test_seconds_since(self) -> None:
        assert seconds_since(NOW - timedelta(seconds=30), now=NOW) == 30.0

    def test_hours_since(self) -> None:
        assert hours_since(NOW - timedelta(hours=3), now=NOW) == 3.0

    def test_none_yields_none(self) -> None:
        assert seconds_since(None) is None
        assert hours_since(None) is None

    def test_naive_input_is_accepted(self) -> None:
        naive = (NOW - timedelta(hours=2)).replace(tzinfo=None)
        assert hours_since(naive, now=NOW) == 2.0

    def test_future_timestamps_are_negative(self) -> None:
        assert seconds_since(NOW + timedelta(hours=1), now=NOW) == -3600.0


class TestSafeDiv:
    @pytest.mark.parametrize(
        ("numerator", "denominator", "expected"),
        [(10, 2, 5.0), (10.0, 4, 2.5), (-10, 4, -2.5), (0, 5, 0.0)],
    )
    def test_ordinary_division(self, numerator: float, denominator: float, expected: float) -> None:
        assert safe_div(numerator, denominator) == expected

    @pytest.mark.parametrize(
        ("numerator", "denominator"),
        [(None, 5), (5, None), (5, 0), (None, 0), (5, 0.0)],
    )
    def test_undefined_division_is_none_not_infinity(
        self, numerator: float | None, denominator: float | None
    ) -> None:
        assert safe_div(numerator, denominator) is None

    def test_non_finite_inputs(self) -> None:
        assert safe_div(float("inf"), 2) is None
        assert safe_div(float("nan"), 2) is None


class TestPercentChange:
    @pytest.mark.parametrize(
        ("current", "previous", "expected"),
        [
            (2_000, 1_000, 100.0),
            (500, 1_000, -50.0),
            (1_000, 1_000, 0.0),
            (1_000, -1_000, 200.0),
        ],
    )
    def test_change(self, current: float, previous: float, expected: float) -> None:
        assert percent_change(current, previous) == expected

    @pytest.mark.parametrize(("current", "previous"), [(None, 1), (1, None), (None, None)])
    def test_missing_side_is_none(self, current: None, previous: float | None) -> None:
        assert percent_change(current, previous) is None

    def test_growth_from_zero_previous_is_none(self) -> None:
        """Dividing by zero must not report infinite growth."""
        assert percent_change(100, 0) is None


class TestFormatCompact:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "n/a"),
            (0, "0"),
            (7, "7"),
            (999, "999"),
            (1_000, "1K"),
            (1_234, "1.2K"),
            (1_500, "1.5K"),
            (999_999, "1M"),
            (3_200_000, "3.2M"),
            (1_000_000_000, "1B"),
            (2_500_000_000_000, "2.5T"),
        ],
    )
    def test_magnitudes(self, value: float | None, expected: str) -> None:
        assert format_compact(value) == expected

    def test_unit_always_matches_the_magnitude(self) -> None:
        """Regression risk: ``7`` must never render as ``0K``."""
        for value in range(0, 5_000):
            text = format_compact(value)
            assert "0K" not in text, f"{value} rendered as {text}"

    def test_negative_values(self) -> None:
        assert format_compact(-1_500) == "-1.5K"

    def test_places_and_placeholder_are_configurable(self) -> None:
        assert format_compact(1_234, places=2) == "1.23K"
        assert format_compact(None, unavailable="-") == "-"


class TestFormatPct:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, "n/a"), (94.312, "+94.31%"), (-12.5, "-12.50%"), (0.0, "0.00%")],
    )
    def test_percentages(self, value: float | None, expected: str) -> None:
        assert format_pct(value) == expected

    def test_unsigned_drops_the_plus(self) -> None:
        assert format_pct(5.0, signed=False) == "5.00%"


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (None, "n/a"),
            (0, "0s"),
            (45, "45s"),
            (60, "1m 0s"),
            (125, "2m 5s"),
            (3_600, "1h 0m"),
            (7_500, "2h 5m"),
            (86_400, "1d 0h"),
            (172_800, "2d 0h"),
        ],
    )
    def test_durations(self, seconds: int | None, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestRelativeTime:
    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=5), "just now"),
            (timedelta(minutes=1), "just now"),
            (timedelta(hours=3), "3h 0m ago"),
            (timedelta(days=2), "2d 0h ago"),
        ],
    )
    def test_descriptions(self, delta: timedelta, expected: str) -> None:
        assert relative_time(NOW - delta, now=NOW) == expected

    def test_none_reads_as_unknown(self) -> None:
        assert relative_time(None) == "unknown"

    def test_clock_skew_does_not_produce_negative_ages(self) -> None:
        assert relative_time(NOW + timedelta(hours=1), now=NOW) == "just now"


class TestRobloxTimestamps:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-03-01T12:00:00.000Z", datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
            ("2026-03-01T12:00:00Z", datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
            ("2026-03-01T14:00:00+02:00", datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
            ("  2026-03-01T12:00:00Z  ", datetime(2026, 3, 1, 12, 0, tzinfo=UTC)),
        ],
    )
    def test_valid(self, raw: str, expected: datetime) -> None:
        assert parse_roblox_timestamp(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, 123, "not a date", "2026-13-45T99:99:99Z", [], {}])
    def test_invalid_returns_none(self, raw: object) -> None:
        assert parse_roblox_timestamp(raw) is None


class TestSerialisation:
    def test_isoformat(self) -> None:
        assert isoformat(NOW) == "2026-03-01T12:00:00+00:00"

    def test_isoformat_of_none(self) -> None:
        assert isoformat(None) is None

    def test_day_bounds_are_midnight_to_midnight(self) -> None:
        start, end = day_bounds(NOW)
        assert start == datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        assert end == datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
        assert (end - start) == timedelta(days=1)

    def test_day_bounds_stay_in_utc(self) -> None:
        start, _ = day_bounds(datetime(2026, 3, 1, 23, 59, tzinfo=timezone(timedelta(hours=5))))
        assert start.tzinfo is not None
        assert start.utcoffset() == timedelta(0)
