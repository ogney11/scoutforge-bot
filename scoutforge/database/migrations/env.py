"""Alembic environment for ScoutForge.

Uses the application's async engine and metadata, so migrations and the ORM
models can never drift apart.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(ROOT))

from scoutforge.database.models import Base  # noqa: E402

config = context.config

if config.config_file_name and Path(config.config_file_name).exists():
    # Installed as a wheel there is no alembic.ini next to the package; logging
    # configuration is optional, the settings below are what actually matter.
    fileConfig(config.config_file_name)

DATABASE_URL = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if DATABASE_URL:
    # Production is MySQL via the async asyncmy driver. Alembic can drive an
    # async engine itself (``run_sync`` below), so nothing needs rewriting here;
    # a fully synchronous URL is passed through untouched.
    config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (``alembic upgrade --sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations through an async engine (MySQL/asyncmy and sqlite both)."""
    url = config.get_main_option("sqlalchemy.url") or ""
    if not url:
        raise RuntimeError("sqlalchemy.url is not configured for migrations")
    engine = create_async_engine(url, poolclass=None, future=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


#: URL schemes that only exist for an async driver.
_ASYNC_DRIVERS = ("+asyncmy", "+aiosqlite", "+asyncpg")


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    url = config.get_main_option("sqlalchemy.url") or ""
    if any(marker in url for marker in _ASYNC_DRIVERS):
        asyncio.run(run_async_migrations())
    else:  # pragma: no cover - sync driver path
        from sqlalchemy import engine_from_config, pool

        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
