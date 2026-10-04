"""Database engine / session management and migration entrypoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from scoutforge.config.logging import get_logger, log_event
from scoutforge.config.settings import DatabaseSettings, get_settings
from scoutforge.database.models import Base

log = get_logger(__name__)


def create_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    """Create the async engine described by the configuration.

    Pool sizing only applies to a server-backed database: sqlite uses its own
    single-connection pools and rejects ``pool_size``/``max_overflow`` outright.
    """
    settings = settings or get_settings().database
    kwargs: dict[str, object] = {
        "echo": settings.echo,
        "pool_pre_ping": True,
        "future": True,
    }
    if not settings.is_sqlite:
        kwargs.update(
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout_seconds,
            pool_recycle=settings.pool_recycle_seconds,
        )
        if "+asyncmy" in settings.database_url:
            # READ COMMITTED keeps a long discovery scan from holding read locks
            # against the dashboard's short reads. asyncmy has no
            # ``isolation_level`` connect kwarg (unlike aiomysql), so the level is
            # set per connection via ``init_command``.
            connect_args: dict[str, object] = {
                "autocommit": False,
            }
            if settings.isolation_level:
                connect_args["init_command"] = (
                    f"SET SESSION TRANSACTION ISOLATION LEVEL {settings.isolation_level}"
                )
            kwargs["connect_args"] = connect_args
    engine = create_async_engine(settings.database_url, **kwargs)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create a session factory bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Transactional session scope: commit on success, rollback on error."""
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Commit/rollback around an *existing* session (the caller still closes it)."""
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def connect_with_retry(engine: AsyncEngine, retries: int = 5) -> None:
    """Wait for the database to accept connections (docker-compose startup race)."""
    delay = 1.0
    for attempt in range(1, max(1, retries) + 1):
        try:
            async with engine.connect():
                log_event(log, 20, "DB_CONNECTED", attempt=attempt)
                return
        except Exception as exc:
            if attempt >= retries:
                log_event(log, 40, "DB_CONNECT_FAILED", attempts=attempt, error=type(exc).__name__)
                raise
            log_event(
                log,
                25,
                "DB_CONNECT_RETRY",
                attempt=attempt,
                error=type(exc).__name__,
                retry_in=round(delay, 1),
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


async def create_all(engine: AsyncEngine) -> None:
    """Create the schema directly. Intended for tests and first-run bootstrap."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log_event(log, 20, "DB_SCHEMA_CREATED")


async def drop_all(engine: AsyncEngine) -> None:
    """Drop every ScoutForge table. Destructive - tests and local dev only."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


__all__ = [
    "connect_with_retry",
    "create_all",
    "create_engine",
    "create_session_factory",
    "drop_all",
    "session_scope",
    "transaction",
]
