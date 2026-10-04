"""Snapshot recording.

ScoutForge only ever stores metrics it actually observed. A snapshot row is
written for every scan in which the game was successfully enriched - there is
no back-filling, no interpolation and no invented history. Consequently growth
numbers only appear once real history exists, and the UI says so until then.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.database.models import GameSnapshot
from scoutforge.database.repositories import GameSnapshotRepository
from scoutforge.discovery.models import GameDetails
from scoutforge.utils import utcnow

log = get_logger(__name__)

#: Snapshots closer than this are collapsed - avoids a wall of near-identical rows.
MIN_SNAPSHOT_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class SnapshotWriteResult:
    written: int = 0
    skipped: int = 0
    captured_at: datetime | None = None


class SnapshotRecorder:
    """Persists metric observations."""

    def __init__(self, repository: GameSnapshotRepository) -> None:
        self.repository = repository

    async def record(
        self,
        details: Sequence[GameDetails],
        *,
        source: str | None = None,
        captured_at: datetime | None = None,
        force: bool = False,
    ) -> SnapshotWriteResult:
        """Write one snapshot per successfully enriched game."""
        captured_at = captured_at or utcnow()
        result = SnapshotWriteResult(captured_at=captured_at)
        rows: list[dict[str, Any]] = []

        for detail in details:
            last = await self.repository.latest(detail.universe_id)
            if not force and last is not None:
                age = (captured_at - last.captured_at).total_seconds()
                if age < MIN_SNAPSHOT_INTERVAL_SECONDS:
                    result.skipped += 1
                    continue
            rows.append(self._row(detail, source=source, captured_at=captured_at))

        result.written = await self.repository.add_many(rows)
        if result.written:
            log_event(
                log,
                20,
                "SNAPSHOTS",
                written=result.written,
                skipped=result.skipped,
            )
        return result

    @staticmethod
    def _row(details: GameDetails, *, source: str | None, captured_at: datetime) -> dict[str, Any]:
        return {
            "universe_id": details.universe_id,
            "captured_at": captured_at,
            "current_ccu": details.current_ccu,
            "lifetime_visits": details.lifetime_visits,
            "favorites": details.favorites,
            "likes": details.likes,
            "dislikes": details.dislikes,
            "playing_now": details.playing_now,
            "voice_chat_visits": details.voice_chat_visits,
            "source": source,
        }

    async def prune(self, retention_days: int) -> int:
        deleted = await self.repository.prune(retention_days)
        if deleted:
            log_event(log, 20, "SNAPSHOTS_PRUNED", deleted=deleted, retention_days=retention_days)
        return deleted


class SnapshotReader:
    """Read-side helpers for charts and growth maths."""

    def __init__(self, repository: GameSnapshotRepository) -> None:
        self.repository = repository

    async def series(
        self,
        universe_id: int,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[GameSnapshot]:
        return await self.repository.window(universe_id, since=since, limit=limit)

    async def to_chart_points(
        self, snapshots: Sequence[GameSnapshot], *, max_points: int = 60
    ) -> list[dict[str, Any]]:
        """Down-sample a snapshot series into chart-ready points."""
        if not snapshots:
            return []
        step = max(1, len(snapshots) // max_points)
        sampled = list(snapshots[::step])[-max_points:]
        return [
            {
                "t": snap.captured_at.isoformat(),
                "ccu": snap.current_ccu,
                "visits": snap.lifetime_visits,
                "favorites": snap.favorites,
            }
            for snap in sampled
            if snap.current_ccu is not None or snap.lifetime_visits is not None
        ]

    async def window_start(self, universe_id: int, *, hours: float) -> datetime:
        return utcnow() - timedelta(hours=hours)


async def count_snapshots(session: AsyncSession, universe_id: int) -> int:
    """Convenience helper used by the services layer."""
    repo = GameSnapshotRepository(session)
    return await repo.count_for(universe_id)


__all__ = [
    "MIN_SNAPSHOT_INTERVAL_SECONDS",
    "SnapshotReader",
    "SnapshotRecorder",
    "SnapshotWriteResult",
    "count_snapshots",
]
