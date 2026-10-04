"""Demo seeder integration tests.

The seeder writes into the same tables the worker does, so its output has to be
indistinguishable in *shape* from real data and unmistakable in *origin*. The
guard against seeding a production database is the part worth testing hard.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scoutforge.config.settings import (
    AlertSettings,
    DatabaseSettings,
    DiscordSettings,
    FilterSettings,
    RobloxApiSettings,
    ScoreWeights,
    Settings,
    WorkerSettings,
)
from scoutforge.database.models import Alert, Base, Game, GameScore, GameSnapshot
from scoutforge.database.session import create_engine, create_session_factory
from scoutforge.enums import AlertLevel, LeadStatus
from scoutforge.scripts.seed_demo import DEMO_PREFIX, DEMO_SOURCE, seed_demo


@pytest.fixture
def demo_settings(tmp_path: Path) -> Settings:
    """Development settings against a throwaway SQLite file."""
    return Settings(
        _env_file=None,
        environment="development",
        log_level="WARNING",
        log_json=False,
        discord=DiscordSettings(token="x"),
        database=DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"),
        filters=FilterSettings(),
        alerts=AlertSettings(),
        worker=WorkerSettings(),
        roblox=RobloxApiSettings(),
        score=ScoreWeights(),
    )


@pytest.fixture
async def demo_factory(
    demo_settings: Settings,
) -> tuple[async_sessionmaker[AsyncSession], Settings]:
    engine = create_engine(demo_settings.database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine), demo_settings
    await engine.dispose()


async def _count(session: AsyncSession, model: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


class TestSeeding:
    async def test_seeds_games_and_returns_the_count(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        seeded = await seed_demo(settings)
        assert seeded > 0

        async with factory() as session:
            assert await _count(session, Game) == seeded

    async def test_every_row_is_unmistakably_demo_data(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        """Real scouts must never mistake a fixture for a real lead."""
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            games = list((await session.execute(select(Game))).scalars().all())

        assert games, "expected demo games"
        for game in games:
            assert game.game_name is not None
            assert game.game_name.startswith(DEMO_PREFIX), game.game_name
            assert game.discovery_source == DEMO_SOURCE
            assert (game.description or "").startswith("[DEMO DATA]")

    async def test_history_snapshots_and_scores_exist(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        """A demo lead with no history would make every chart look broken."""
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            assert await _count(session, GameSnapshot) > 0
            assert await _count(session, GameScore) > 0

    async def test_snapshots_are_ordered_and_bounded_per_game(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            rows = list(
                (await session.execute(select(GameSnapshot).order_by(GameSnapshot.captured_at)))
                .scalars()
                .all()
            )
        assert rows
        assert rows[0].captured_at <= rows[-1].captured_at

    async def test_pipeline_states_are_real_and_legal(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            statuses = {status.value for status in LeadStatus}
            games = list((await session.execute(select(Game))).scalars().all())

        assert games
        assert {game.status for game in games} <= statuses

    async def test_alerts_reference_seeded_levels(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            rows = list((await session.execute(select(Alert))).scalars().all())

        assert rows
        assert all(row.level in {level.value for level in AlertLevel} for row in rows)

    async def test_leads_are_flagged_as_leads(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            games = list((await session.execute(select(Game))).scalars().all())

        assert any(game.is_lead for game in games)


class TestIdempotency:
    async def test_second_run_seeds_nothing_new(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        first = await seed_demo(settings)

        async with factory() as session:
            snapshots_after_first = await _count(session, GameSnapshot)

        second = await seed_demo(settings)
        assert second == 0, "existing universes must be skipped, not duplicated"

        async with factory() as session:
            assert await _count(session, Game) == first
            assert await _count(session, GameSnapshot) == snapshots_after_first

    async def test_third_run_is_still_stable(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        factory, settings = demo_factory
        await seed_demo(settings)
        await seed_demo(settings)
        assert await seed_demo(settings) == 0

        async with factory() as session:
            assert await _count(session, Game) == await _count(session, Game)


class TestProductionGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "mysql+asyncmy://u:p@mysql:3306/scoutforge",
            "mysql+asyncmy://u:p@scoutforge-db:3306/scoutforge",
            "mysql+asyncmy://u:p@prod-db:3306/scoutforge",
            "mysql+asyncmy://u:p@PROD-DB:3306/scoutforge",
            "mysql+asyncmy://u:p@db-primary.scoutforge.internal:3306/scoutforge",
        ],
    )
    async def test_refuses_production_looking_hosts(self, url: str) -> None:
        """The guard must fire before any connection is attempted."""
        settings = Settings(_env_file=None, database=DatabaseSettings(database_url=url))
        with pytest.raises(RuntimeError, match="production"):
            await seed_demo(settings)

    async def test_refuses_production_environment(self, guarded_settings: Settings) -> None:
        settings = guarded_settings.model_copy(update={"environment": "production"})
        with pytest.raises(RuntimeError, match="production"):
            await seed_demo(settings)

    async def test_local_paths_are_not_mistaken_for_production(self, tmp_path: Path) -> None:
        """A scratch path that merely *contains* "prod" must stay seedable.

        The guard only inspects the host, so temporary directories named after the
        test that created them never turn into a reason to pass ``force=True``.
        """
        scratch = tmp_path / "production-regression"
        scratch.mkdir()
        settings = Settings(
            _env_file=None,
            environment="development",
            database=DatabaseSettings(database_url=f"sqlite+aiosqlite:///{scratch / 'sf.db'}"),
        )
        engine = create_engine(settings.database)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

        assert await seed_demo(settings) > 0

    async def test_allows_a_local_development_database(self, guarded_settings: Settings) -> None:
        assert await seed_demo(guarded_settings) > 0

    async def test_force_overrides_the_host_guard(self, guarded_settings: Settings) -> None:
        settings = guarded_settings.model_copy(
            update={
                "database": DatabaseSettings(
                    database_url="mysql+asyncmy://u:p@mysql:3306/scoutforge"
                )
            }
        )
        with pytest.raises(RuntimeError, match="production"):
            await seed_demo(settings)

    async def test_force_overrides_the_environment_guard(self, guarded_settings: Settings) -> None:
        settings = guarded_settings.model_copy(update={"environment": "production"})
        assert await seed_demo(settings, force=True) > 0


@pytest.fixture
async def guarded_settings(tmp_path: Path) -> AsyncIterator[Settings]:
    """Development settings against a throwaway, schema-initialised file.

    ``seed_demo`` never creates tables - it assumes migrations already ran - so
    the guard tests need a database that is ready to receive writes.
    """
    settings = Settings(
        _env_file=None,
        environment="development",
        database=DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}"),
    )
    engine = create_engine(settings.database)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield settings


class TestSeededDataIsReadable:
    async def test_seeded_data_is_visible_to_the_read_services(
        self, demo_factory: tuple[async_sessionmaker[AsyncSession], Settings]
    ) -> None:
        """The point of the seeder is data the bot can already read and show."""
        from scoutforge.database.repositories import (
            GameRepository,
            GameScoreRepository,
            StatsRepository,
        )

        factory, settings = demo_factory
        await seed_demo(settings)

        async with factory() as session:
            games = GameRepository(session)
            leads = await games.list_leads()
            scores = GameScoreRepository(session)
            history = await scores.history(leads[0].universe_id)
            stats = StatsRepository(session)
            tracked = await stats.total_games()
            per_day = await stats.discoveries_per_day()

        assert leads
        assert history, "demo history must produce a score series"
        assert tracked > 0
        assert per_day, "demo data must feed the /stats chart"
