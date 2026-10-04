"""Growth and activity analysis built purely on collected snapshots.

Nothing here estimates values it has not observed. When there is not enough
history the corresponding window is reported as *unavailable* (``None``) and a
caveat explains why, instead of guessing.

Definitions
-----------
``growth_x_pct``
    Percentage change between the newest observation and the oldest observation
    at least ``x`` hours older. Uses two real snapshots, never a projection.
``ccus_per_day``
    Absolute CCU gained per day across the observed window.
``stability``
    ``1 - (max-min)/max`` of CCU across the window: 1.0 means rock steady.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from scoutforge.database.models import GameSnapshot
from scoutforge.utils import ensure_utc, percent_change, utcnow


@dataclass(slots=True)
class GrowthWindow:
    """Change between two observed snapshots."""

    window_hours: int
    pct_change: float
    absolute_change: int
    from_ccu: int
    to_ccu: int
    samples: int
    from_at: datetime
    to_at: datetime

    @property
    def hours_observed(self) -> float:
        return (self.to_at - self.from_at).total_seconds() / 3600.0


@dataclass(slots=True)
class GrowthMetrics:
    """All growth signals available for one game."""

    universe_id: int
    snapshot_count: int = 0
    windows: dict[int, GrowthWindow] = field(default_factory=dict)
    stability: float | None = None
    consistency_samples: int = 0
    ccus_per_day: float | None = None
    peak_ccu: int | None = None
    observed_hours: float = 0.0
    caveats: list[str] = field(default_factory=list)

    @property
    def has_growth_data(self) -> bool:
        return bool(self.windows)

    @property
    def has_consistency_data(self) -> bool:
        return self.stability is not None and self.consistency_samples >= 3

    def window(self, hours: int) -> GrowthWindow | None:
        return self.windows.get(hours)

    @property
    def growth_1h_pct(self) -> float | None:
        win = self.windows.get(1)
        return win.pct_change if win else None

    @property
    def growth_6h_pct(self) -> float | None:
        win = self.windows.get(6)
        return win.pct_change if win else None

    @property
    def growth_24h_pct(self) -> float | None:
        win = self.windows.get(24)
        return win.pct_change if win else None

    @property
    def primary_growth(self) -> GrowthWindow | None:
        """Best available growth window: 24h, else 6h, else 1h."""
        for hours in (24, 6, 1):
            win = self.windows.get(hours)
            if win is not None:
                return win
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_count": self.snapshot_count,
            "windows": {
                str(hours): {
                    "pct_change": round(win.pct_change, 2),
                    "absolute_change": win.absolute_change,
                    "from_ccu": win.from_ccu,
                    "to_ccu": win.to_ccu,
                    "samples": win.samples,
                }
                for hours, win in self.windows.items()
            },
            "stability": round(self.stability, 3) if self.stability is not None else None,
            "consistency_samples": self.consistency_samples,
            "ccus_per_day": round(self.ccus_per_day, 2) if self.ccus_per_day is not None else None,
            "peak_ccu": self.peak_ccu,
            "observed_hours": round(self.observed_hours, 2),
            "caveats": list(self.caveats),
        }


def compute_growth(
    universe_id: int,
    snapshots: Sequence[GameSnapshot],
    *,
    now: datetime | None = None,
    windows: Sequence[int] = (1, 6, 24),
) -> GrowthMetrics:
    """Compute growth metrics from an ordered snapshot series.

    Args:
        universe_id: Universe the snapshots belong to (used for messages).
        snapshots: Snapshots ordered oldest -> newest.
        now: Injectable clock for deterministic tests.
        windows: Growth window sizes in hours to attempt.
    """
    now = ensure_utc(now) or utcnow()
    ordered = sorted(
        (s for s in snapshots if s.current_ccu is not None),
        key=lambda s: ensure_utc(s.captured_at) or now,
    )
    metrics = GrowthMetrics(universe_id=universe_id, snapshot_count=len(snapshots))

    if not ordered:
        metrics.caveats.append("No CCU history collected yet")
        return metrics

    if len(ordered) < 2:
        metrics.caveats.append("Only one observation so far - growth not measurable")
        return metrics

    first, last = ordered[0], ordered[-1]
    observed_hours = (
        (ensure_utc(last.captured_at) or now) - (ensure_utc(first.captured_at) or now)
    ).total_seconds() / 3600.0
    metrics.observed_hours = observed_hours
    metrics.peak_ccu = max(int(s.current_ccu or 0) for s in ordered)

    for hours in windows:
        window = _window_for(ordered, hours=hours, now=now)
        if window is not None:
            metrics.windows[hours] = window

    # Name every window we could not compute. A bare ``None`` growth figure with
    # no explanation reads as "no growth" rather than "not enough history".
    missing = [hours for hours in windows if hours not in metrics.windows]
    if missing:
        if not metrics.windows:
            metrics.caveats.append(
                f"History spans only {observed_hours:.1f}h - not enough for a "
                f"{min(missing)}h comparison"
            )
        else:
            listed = ", ".join(f"{hours}h" for hours in sorted(missing))
            metrics.caveats.append(
                f"Not enough history for the {listed} comparison ({observed_hours:.1f}h tracked)"
            )

    ccus = [int(s.current_ccu or 0) for s in ordered[-24:]]
    if len(ccus) >= 3:
        metrics.consistency_samples = len(ccus)
        metrics.stability = _stability(ccus)
    else:
        metrics.caveats.append(
            f"Only {len(ccus)} CCU observations - activity consistency not measurable"
        )

    if observed_hours >= 1.0:
        span_days = observed_hours / 24.0
        metrics.ccus_per_day = (
            int(last.current_ccu or 0) - int(first.current_ccu or 0)
        ) / span_days
    else:
        metrics.caveats.append("Tracking window under 1h - growth velocity not meaningful yet")

    return metrics


def _window_for(
    ordered: Sequence[GameSnapshot], *, hours: int, now: datetime
) -> GrowthWindow | None:
    """Find the newest snapshot and the oldest one at least ``hours`` earlier."""
    newest = ordered[-1]
    newest_at = ensure_utc(newest.captured_at) or now
    cutoff = newest_at - timedelta(hours=hours)
    baseline = None
    for snap in ordered:
        captured = ensure_utc(snap.captured_at) or now
        if captured <= cutoff:
            baseline = snap
        else:
            break
    if baseline is None:
        return None

    from_ccu = int(baseline.current_ccu or 0)
    to_ccu = int(newest.current_ccu or 0)
    pct = percent_change(to_ccu, from_ccu)
    if pct is None:
        return None
    return GrowthWindow(
        window_hours=hours,
        pct_change=pct,
        absolute_change=to_ccu - from_ccu,
        from_ccu=from_ccu,
        to_ccu=to_ccu,
        samples=len(ordered),
        from_at=ensure_utc(baseline.captured_at) or now,
        to_at=newest_at,
    )


#: Residual volatility (as a share of average CCU) at which stability reaches 0.
_STABILITY_VOLATILITY_CEILING = 0.40


def _stability(ccus: Sequence[int]) -> float:
    """How predictable the audience is: 1.0 = steady, 0.0 = erratic.

    Measured as the RMS residual around a least-squares trend line, relative to
    the average audience size. Range is deliberately *not* used: a game that
    climbs steadily is predictable, and scoring it as "erratic" would contradict
    the growth component, which exists to reward exactly that climb.

    Examples (24h window):
      * 5,000 CCU oscillating +/-2,000 around flat -> ~0.0 (a spike, not a baseline)
      * 5,000 climbing steadily to 13,000        -> ~1.0 (a real, growing audience)
      * 5,000 +/-1% noise, no trend             -> ~1.0
    """
    values = [float(v) for v in ccus if v is not None]
    count = len(values)
    if count < 3:
        return 0.0

    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    variance_x = sum((i - mean_x) ** 2 for i in range(count))
    if variance_x == 0:
        return 0.0

    slope = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / variance_x
    intercept = mean_y - slope * mean_x
    residuals = [v - (intercept + slope * i) for i, v in enumerate(values)]
    rms = math.sqrt(sum(r * r for r in residuals) / count)

    scale = abs(mean_y) if mean_y else 1.0
    relative = rms / scale / _STABILITY_VOLATILITY_CEILING
    return max(0.0, min(1.0, 1.0 - relative))


def detect_spike(
    previous_ccu: int | None,
    current_ccu: int | None,
    *,
    threshold_pct: float,
) -> float | None:
    """Return the CCU spike percentage when it exceeds ``threshold_pct``.

    The rule is deliberately percentage-only: the alerting contract is "CCU up
    50% or more", and adding a separate absolute-jump rule would fire alerts the
    operator never asked for. ``None`` means no qualifying spike.
    """
    pct = percent_change(current_ccu, previous_ccu)
    if pct is None:
        return None
    if pct >= threshold_pct:
        return pct
    return None


__all__ = ["GrowthMetrics", "GrowthWindow", "compute_growth", "detect_spike"]
