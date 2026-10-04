"""Aggregate statistics for ``/stats`` and the dashboard.

Every number here is derived from stored rows (``discovery_runs``, ``alerts``,
``server_stats``, ``games``) - nothing is estimated or carried over from memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger
from scoutforge.database.repositories import (
    AlertRepository,
    DiscoveryRunRepository,
    StatsRepository,
)
from scoutforge.enums import AlertLevel, LeadStatus
from scoutforge.utils import format_duration, utcnow

log = get_logger(__name__)


@dataclass(slots=True)
class StatsSnapshot:
    """Everything ``/stats`` renders."""

    scans_completed: int = 0
    scans_24h: int = 0
    candidates_found: int = 0
    games_discovered: int = 0
    games_tracked: int = 0
    leads_total: int = 0
    snapshots_written: int = 0
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    alerts_by_level: dict[str, int] = field(default_factory=dict)
    leads_by_status: dict[str, int] = field(default_factory=dict)
    hot_leads: int = 0
    extreme_leads: int = 0
    warm_leads: int = 0
    api_errors: int = 0
    rate_limited: int = 0
    http_requests: int = 0
    average_scan_duration_ms: float = 0.0
    last_scan_at: Any = None
    discoveries_per_day: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scans_completed": self.scans_completed,
            "scans_24h": self.scans_24h,
            "candidates_found": self.candidates_found,
            "games_discovered": self.games_discovered,
            "games_tracked": self.games_tracked,
            "leads_total": self.leads_total,
            "snapshots_written": self.snapshots_written,
            "alerts_sent": self.alerts_sent,
            "alerts_suppressed": self.alerts_suppressed,
            "alerts_by_level": self.alerts_by_level,
            "leads_by_status": self.leads_by_status,
            "hot_leads": self.hot_leads,
            "extreme_leads": self.extreme_leads,
            "warm_leads": self.warm_leads,
            "api_errors": self.api_errors,
            "rate_limited": self.rate_limited,
            "http_requests": self.http_requests,
            "average_scan_duration_ms": self.average_scan_duration_ms,
            "average_scan_duration": format_duration((self.average_scan_duration_ms or 0) / 1000.0),
            "last_scan_at": self.last_scan_at.isoformat() if self.last_scan_at else None,
            "discoveries_per_day": self.discoveries_per_day,
        }


class StatsService:
    """Builds a :class:`StatsSnapshot` from the database."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.runs = DiscoveryRunRepository(session)
        self.alerts = AlertRepository(session)
        self.stats = StatsRepository(session)

    async def snapshot(self, *, days: int = 14) -> StatsSnapshot:
        since = utcnow() - timedelta(days=days)
        summary = await self.runs.summary(since=since)
        recent_runs = await self.runs.recent(limit=1)
        since_24h = utcnow() - timedelta(hours=24)

        snapshot = StatsSnapshot(
            scans_completed=int(summary["scans_completed"]),
            candidates_found=int(summary["candidates_found"]),
            games_discovered=int(summary["games_discovered"]),
            snapshots_written=int(summary["snapshots_written"]),
            alerts_sent=int(summary["alerts_sent"]),
            alerts_suppressed=int(summary["alerts_suppressed"]),
            api_errors=int(summary["api_errors"]),
            average_scan_duration_ms=float(summary["average_scan_duration_ms"]),
            alerts_by_level=await self.alerts.counts_by_level(),
            leads_by_status=await self._status_counts(),
            games_tracked=await self.stats.total_games(),
            discoveries_per_day=await self.stats.discoveries_per_day(days=days),
        )

        if recent_runs:
            snapshot.last_scan_at = recent_runs[0].started_at
        snapshot.scans_24h = len(
            [
                run
                for run in await self.runs.recent(limit=1000)
                if run.started_at and since_24h <= run.started_at
            ]
        )

        counters = await self.stats.snapshot()
        snapshot.leads_total = sum(snapshot.leads_by_status.values())
        snapshot.http_requests = int(counters.get("http_requests", 0))
        snapshot.rate_limited = int(counters.get("http_rate_limited", 0))
        levels = await self.level_breakdown()
        snapshot.hot_leads = int(levels.get(AlertLevel.HOT.value, 0))
        snapshot.extreme_leads = int(levels.get(AlertLevel.EXTREME.value, 0))
        snapshot.warm_leads = int(levels.get(AlertLevel.WARM.value, 0))
        return snapshot

    async def _status_counts(self) -> dict[str, int]:
        from scoutforge.database.repositories import GameRepository

        return await GameRepository(self.session).status_counts()

    async def funnel_counts(self) -> dict[str, Any]:
        """Pipeline funnel: discovered -> leads -> contacted -> interested -> deals."""
        counts = await self._status_counts()
        contacted = sum(
            counts.get(status.value, 0)
            for status in (
                LeadStatus.CONTACTED,
                LeadStatus.REPLIED,
                LeadStatus.INTERESTED,
                LeadStatus.SENT_TO_WORLDENT,
                LeadStatus.NEGOTIATING,
                LeadStatus.DEAL,
            )
        )
        return {
            "discovered": await self.stats.total_games(),
            "leads": sum(counts.values()),
            "new": counts.get(LeadStatus.NEW.value, 0),
            "researched": counts.get(LeadStatus.RESEARCHED.value, 0),
            "contacted": contacted,
            "interested": counts.get(LeadStatus.INTERESTED.value, 0),
            "sent_to_worldent": counts.get(LeadStatus.SENT_TO_WORLDENT.value, 0),
            "negotiating": counts.get(LeadStatus.NEGOTIATING.value, 0),
            "deals": counts.get(LeadStatus.DEAL.value, 0),
            "rejected": counts.get(LeadStatus.REJECTED.value, 0),
            "not_interested": counts.get(LeadStatus.NOT_INTERESTED.value, 0),
        }

    async def level_breakdown(self) -> dict[str, int]:
        from scoutforge.database.repositories import GameRepository

        return await GameRepository(self.session).level_counts()


__all__ = ["AlertLevel", "StatsService", "StatsSnapshot"]
