"""Hard filters.

These are *gates*, not scoring: a candidate either passes into the database and
the scoring pipeline, or it is rejected with a machine-readable reason (useful
for ``/stats`` and the dashboard).

All thresholds come from :class:`~scoutforge.config.settings.FilterSettings` -
nothing is hard-coded here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from scoutforge.config.settings import FilterSettings
from scoutforge.discovery.models import GameDetails
from scoutforge.utils import ensure_utc, hours_since, safe_div, utcnow


class RejectionReason(StrEnum):
    """Why a candidate did not make it into the funnel."""

    MISSING_UNIVERSE_ID = "missing_universe_id"
    MISSING_CREATOR = "missing_creator"
    UNIDENTIFIABLE = "unidentifiable"
    PRIVATE_GAME = "private_game"
    BELOW_MIN_CCU = "below_min_ccu"
    ABOVE_MAX_VISITS = "above_max_lifetime_visits"
    MISSING_CCU = "missing_ccu"
    MISSING_VISITS = "missing_lifetime_visits"
    NON_POSITIVE_CCU = "non_positive_ccu"
    ABANDONED = "abandoned"
    DUPLICATE = "duplicate"
    ALREADY_TRACKED = "already_tracked"


@dataclass(slots=True)
class FilterVerdict:
    """Outcome of applying the hard filters to one game."""

    passed: bool
    reason: RejectionReason | None = None
    detail: str | None = None
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


class GameFilter:
    """Applies the configured hard rules to enriched game details."""

    def __init__(self, settings: FilterSettings | None = None) -> None:
        self.settings = settings or FilterSettings()

    # ------------------------------------------------------------------ api
    def evaluate(
        self,
        details: GameDetails,
        *,
        now: datetime | None = None,
        history: dict[str, object] | None = None,
    ) -> FilterVerdict:
        """Decide whether ``details`` should become a tracked lead.

        Args:
            details: Enriched game payload.
            now: Injectable clock for deterministic tests.
            history: Optional prior observations. Recognised keys:
                ``first_seen_at``, ``last_ccu``, ``last_seen_at``.

        Note:
            ``MAX_LIFETIME_VISITS`` is opt-in. At the default of ``0`` an
            established game with millions of lifetime visits is *not* rejected:
            size alone says nothing about whether a partner would want it, and
            the Scout Score already accounts for audience size. Set the knob
            above zero only if you specifically want a hard size ceiling.
        """
        settings = self.settings
        now = ensure_utc(now) or utcnow()
        history = history or {}
        warnings: list[str] = []

        if not details.universe_id or details.universe_id <= 0:
            return FilterVerdict(False, RejectionReason.MISSING_UNIVERSE_ID)

        if settings.require_universe_id and details.universe_id <= 0:
            return FilterVerdict(False, RejectionReason.MISSING_UNIVERSE_ID)

        if not details.name and not details.place_id:
            return FilterVerdict(False, RejectionReason.UNIDENTIFIABLE, "no name and no place id")

        if settings.require_creator_id and not details.creator.creator_id:
            return FilterVerdict(False, RejectionReason.MISSING_CREATOR, "creator id unavailable")

        if details.is_private:
            return FilterVerdict(False, RejectionReason.PRIVATE_GAME)

        if details.current_ccu is None:
            warnings.append("CCU unavailable from upstream; cannot confirm MIN_CCU")
            return FilterVerdict(False, RejectionReason.MISSING_CCU, warnings=warnings)

        if details.current_ccu <= 0:
            return FilterVerdict(False, RejectionReason.NON_POSITIVE_CCU, str(details.current_ccu))

        if details.current_ccu < settings.min_ccu:
            return FilterVerdict(
                False,
                RejectionReason.BELOW_MIN_CCU,
                f"ccu={details.current_ccu} < MIN_CCU={settings.min_ccu}",
            )

        if details.lifetime_visits is None:
            warnings.append("lifetime visits unavailable from upstream")
            return FilterVerdict(False, RejectionReason.MISSING_VISITS, warnings=warnings)

        if settings.max_lifetime_visits > 0 and (
            details.lifetime_visits >= settings.max_lifetime_visits
        ):
            return FilterVerdict(
                False,
                RejectionReason.ABOVE_MAX_VISITS,
                f"visits={details.lifetime_visits} >= MAX_LIFETIME_VISITS="
                f"{settings.max_lifetime_visits}",
            )

        abandoned = self._is_abandoned(details, history=history, now=now)
        if abandoned is not None:
            return FilterVerdict(False, RejectionReason.ABANDONED, abandoned)

        return FilterVerdict(True, None, None, warnings)

    def evaluate_many(
        self, details: list[GameDetails], **kwargs: object
    ) -> dict[int, FilterVerdict]:
        return {item.universe_id: self.evaluate(item, **kwargs) for item in details}  # type: ignore[arg-type]

    # -------------------------------------------------------------- internal
    def _is_abandoned(
        self, details: GameDetails, *, history: dict[str, object], now: datetime
    ) -> str | None:
        """Detect obviously abandoned games.

        A game is considered abandoned when all of the following hold:

        1. it was first observed more than ``ABANDONED_AFTER_DAYS`` ago,
        2. it is now at or below ``ABANDONED_CCU_THRESHOLD``,
        3. we hold a previous CCU reading showing how busy it used to be.

        Games we have never seen before are never rejected on this basis - there
        is simply no evidence, and inventing some would silently drop real leads.
        """
        settings = self.settings
        if settings.abandoned_after_days <= 0 or settings.abandoned_ccu_threshold <= 0:
            return None

        first_seen = history.get("first_seen_at")
        if not isinstance(first_seen, datetime):
            return None
        seen = ensure_utc(first_seen)
        if seen is None:
            return None
        age_days = (now - seen).total_seconds() / 86400.0
        if age_days < settings.abandoned_after_days:
            return None

        current_ccu = details.current_ccu
        if current_ccu is None or current_ccu > settings.abandoned_ccu_threshold:
            return None

        last_ccu = history.get("last_ccu")
        if not isinstance(last_ccu, int):
            return None
        if last_ccu <= settings.abandoned_ccu_threshold:
            # Two consecutive readings below the threshold: sustained, not a dip.
            return (
                f"ccu stayed at {current_ccu} (was {last_ccu}) "
                f"after {age_days:.0f} days of tracking"
            )
        return (
            f"ccu collapsed from {last_ccu} to {current_ccu} after {age_days:.0f} days of tracking"
        )


def ratio_checks(
    details: GameDetails, *, min_ccu: int, max_visits: int
) -> dict[str, float | int | None]:
    """Return ratio metrics used for reporting (never used as a hard gate)."""
    return {
        "current_ccu": details.current_ccu,
        "lifetime_visits": details.lifetime_visits,
        "ccu_to_visits_ratio": details.ccu_to_visits_ratio,
        "visits_per_day_needed": safe_div(details.lifetime_visits, max_visits)
        if max_visits
        else None,
    }


def is_stale(
    last_seen: datetime | None, *, max_age_seconds: float, now: datetime | None = None
) -> bool:
    """True when an observation is older than ``max_age_seconds``."""
    age = hours_since(last_seen, now=now)
    if age is None:
        return True
    return age * 3600.0 > max_age_seconds


__all__ = [
    "FilterVerdict",
    "GameFilter",
    "RejectionReason",
    "is_stale",
    "ratio_checks",
]
