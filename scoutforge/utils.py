"""Small shared helpers: timezone-aware time handling and display formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

_NUMBER_SUFFIXES = ("", "K", "M", "B", "T")


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC, assuming naive == UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Alias of :func:`ensure_utc` kept for readability at call sites."""
    return ensure_utc(value)


def seconds_since(value: datetime | None, *, now: datetime | None = None) -> float | None:
    """Seconds elapsed since ``value`` (None-safe)."""
    if value is None:
        return None
    reference = now or utcnow()
    delta = reference - ensure_utc(value)  # type: ignore[operator]
    return delta.total_seconds()


def hours_since(value: datetime | None, *, now: datetime | None = None) -> float | None:
    secs = seconds_since(value, now=now)
    return None if secs is None else secs / 3600.0


def safe_div(numerator: float | int | None, denominator: float | int | None) -> float | None:
    """Divide returning ``None`` when the result is undefined or non-finite."""
    if numerator is None or denominator in (None, 0):
        return None
    try:
        result = float(numerator) / float(denominator)
    except (TypeError, ZeroDivisionError, OverflowError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def percent_change(current: float | None, previous: float | None) -> float | None:
    """Percentage change between two measurements (``None`` if undefined)."""
    if current is None or previous is None:
        return None
    change = safe_div(current - previous, abs(previous))
    return None if change is None else change * 100.0


def format_compact(value: float | int | None, *, places: int = 1, unavailable: str = "n/a") -> str:
    """Format a number compactly: ``1234`` -> ``1.2K``, ``3_200_000`` -> ``3.2M``.

    Picks the largest suffix whose scale still fits the magnitude, so the unit
    always matches the number (``7`` stays ``7`` and never becomes ``0K``).
    """
    if value is None:
        return unavailable
    num = float(value)
    negative = num < 0
    num = abs(num)

    index = 0
    while index < len(_NUMBER_SUFFIXES) - 1 and num >= 1000.0 ** (index + 1):
        index += 1

    scaled = num / 1000.0**index
    if index > 0:
        # Rounding can push the mantissa to 1000 (999_999 -> "1000.0K"), so step
        # up once more rather than emit a four-digit value in the wrong unit.
        while index < len(_NUMBER_SUFFIXES) - 1 and round(scaled, places) >= 1000.0:
            index += 1
            scaled = num / 1000.0**index
    text = f"{int(scaled)}" if index == 0 else f"{scaled:.{places}f}".rstrip("0").rstrip(".")
    return f"{'-' if negative else ''}{text}{_NUMBER_SUFFIXES[index]}"


def format_pct(value: float | None, *, places: int = 2, signed: bool = True) -> str:
    """Format a percentage, e.g. ``+94.31%`` or ``n/a``."""
    if value is None:
        return "n/a"
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.{places}f}%"


def format_duration(seconds: float | int | None) -> str:
    """Human readable duration from seconds (``2h 5m``, ``45s``)."""
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def relative_time(value: datetime | None, *, now: datetime | None = None) -> str:
    """Compact relative description such as ``3h ago`` or ``just now``."""
    secs = seconds_since(value, now=now)
    if secs is None:
        return "unknown"
    if secs < 0:
        return "just now"
    if secs < 90:
        return "just now"
    return f"{format_duration(secs)} ago"


def parse_roblox_timestamp(value: Any) -> datetime | None:
    """Parse a Roblox ISO-8601 timestamp (``2024-01-02T03:04:05.000Z``)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ensure_utc(parsed)


def isoformat(value: datetime | None) -> str | None:
    dt = ensure_utc(value)
    return dt.isoformat() if dt else None


def day_bounds(value: datetime) -> tuple[datetime, datetime]:
    """UTC midnight boundaries for the day containing ``value``.

    The input is normalised to UTC first, so a caller in another timezone still
    gets the bounds of the UTC day it actually falls in.
    """
    # ensure_utc() returns None only for a None input, and value is not None.
    moment = cast("datetime", ensure_utc(value))
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


__all__ = [
    "UTC",
    "as_utc",
    "day_bounds",
    "ensure_utc",
    "format_compact",
    "format_duration",
    "format_pct",
    "hours_since",
    "isoformat",
    "parse_roblox_timestamp",
    "percent_change",
    "relative_time",
    "safe_div",
    "seconds_since",
    "utcnow",
]
