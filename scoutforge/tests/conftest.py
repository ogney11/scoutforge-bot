"""Shared pytest fixtures.

The suite runs entirely offline:

* the database is in-memory SQLite carrying the production schema,
* HTTP is mocked with respx, never dialled,
* the wall clock is replaced by :data:`NOW` so growth/freshness maths is exact.

Nothing here may reach Discord, Postgres or the public Roblox API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scoutforge.config.settings import (
    AlertSettings,
    DatabaseSettings,
    DiscordSettings,
    DiscoverySettings,
    FilterSettings,
    RobloxApiSettings,
    ScoreWeights,
    Settings,
    WorkerSettings,
)
from scoutforge.database.models import Base
from scoutforge.enums import AlertLevel, LeadStatus

#: Frozen "current time" for every test in the suite.
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def hours_ago(hours: float, *, base: datetime = NOW) -> datetime:
    """``NOW`` shifted back by ``hours``."""
    return base - timedelta(hours=hours)


@pytest.fixture
def settings() -> Settings:
    """Fully-default settings pointing at test doubles."""
    return Settings(
        _env_file=None,
        environment="development",
        log_level="WARNING",
        log_json=False,
        discord=DiscordSettings(token="test-token"),
        database=DatabaseSettings(database_url="sqlite+aiosqlite:///:memory:"),
        filters=FilterSettings(),
        alerts=AlertSettings(),
        worker=WorkerSettings(),
        roblox=RobloxApiSettings(),
        discovery=DiscoverySettings(),
        score=ScoreWeights(),
    )


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine carrying the production schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as sess:
        yield sess


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def ccu_series() -> Callable[..., list[dict[str, Any]]]:
    """Build a CCU series ending at ``end_ccu``, growing backwards in time."""

    def build(
        end_ccu: int,
        *,
        points: int = 24,
        growth_ratio: float = 0.5,
        step_hours: float = 1.0,
        visits_per_ccu: float = 40.0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index in range(points):
            fraction = index / max(1, points - 1)
            factor = (1.0 - growth_ratio) + growth_ratio * fraction
            ccu = max(0, round(end_ccu * factor))
            rows.append(
                {
                    "captured_at": hours_ago(step_hours * (points - 1 - index)),
                    "current_ccu": ccu,
                    "lifetime_visits": max(0, round(ccu * visits_per_ccu)),
                }
            )
        return rows

    return build


@pytest.fixture
def snapshot_rows() -> Callable[..., list[Any]]:
    """Build persisted-shaped snapshot dicts ready for ``add_many``."""
    from scoutforge.services.snapshot_service import SnapshotService  # noqa: F401

    return lambda rows: [dict(row, source="demo") for row in rows]


@pytest.fixture
def lead_statuses() -> list[str]:
    return [status.value for status in LeadStatus]


@pytest.fixture
def alert_levels() -> list[str]:
    return [level.value for level in AlertLevel]
