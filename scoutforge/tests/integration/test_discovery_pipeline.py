"""Pipeline integration tests for :class:`~scoutforge.services.discovery_service.DiscoveryWorker`.

The single-game pipeline (filter → persist → snapshot → score → **partner match**
→ alert) is where a bad wiring bug hides: each stage passes to the next through a
plain argument, so nothing fails until a real scan runs. ``_process_details`` is
the only seam that exercises steps 4-11 without faking the HTTP layer, so these
tests drive it directly against a real (sqlite) schema.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.settings import (
    AlertSettings,
    DatabaseSettings,
    FilterSettings,
    PartnerSettings,
    Settings,
)
from scoutforge.database.models import Base
from scoutforge.database.repositories import GameRepository, PartnerMatchRepository
from scoutforge.database.session import transaction
from scoutforge.discovery.models import CreatorInfo, GameCandidate, GameDetails
from scoutforge.services.container import Container
from scoutforge.services.discovery_service import MAX_ALERT_PARTNERS, DiscoveryWorker, ScanResult

UNIVERSE = 800_000_001


class _CapturingDispatcher:
    """Stands in for the Discord dispatcher and records payloads."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def send_alert(self, payload: dict[str, Any]) -> int:
        self.payloads.append(payload)
        return 1_000 + len(self.payloads)


def _details(**overrides: Any) -> GameDetails:
    payload: dict[str, Any] = {
        "universe_id": UNIVERSE,
        "place_id": 700_000_001,
        "name": "Steady Grown Original RPG",
        "description": "a handcrafted story adventure with weekly updates",
        "creator": CreatorInfo(
            creator_id=42, creator_name="TestStudio", creator_type="Group", group_id=99
        ),
        "current_ccu": 4_200,
        "lifetime_visits": 1_400_000,
        "favorites": 30_000,
        "likes": 25_000,
        "playing_now": 4_200,
        "max_players": 100,
        "created_at": None,
    }
    payload.update(overrides)
    return GameDetails(**payload)


def _candidate() -> GameCandidate:
    return GameCandidate(
        universe_id=UNIVERSE, place_id=700_000_001, current_ccu=4_200, source="test"
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="development",
        log_level="WARNING",
        log_json=False,
        database=DatabaseSettings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}"),
        alerts=AlertSettings(hot_score=60.0, extreme_score=90.0, warm_score=40.0),
        filters=FilterSettings(min_current_ccu=0, min_lifetime_visits=0),
        partners=PartnerSettings(enabled=True, min_score=30.0, min_game_score=0.0),
    )


@pytest.fixture
async def container(settings: Settings) -> AsyncIterator[Container]:
    instance = Container(settings)
    async with instance.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield instance
    finally:
        await instance.engine.dispose()


async def _run(
    container: Container,
    dispatcher: _CapturingDispatcher,
    details: GameDetails | None = None,
) -> ScanResult:
    result = ScanResult()
    session = container.session()
    worker = DiscoveryWorker(container, dispatcher=dispatcher)  # type: ignore[arg-type]
    async with transaction(session):
        await worker._process_details(
            details=details or _details(),
            candidate=_candidate(),
            result=result,
            session=session,
        )
    await session.close()
    return result


class TestPartnerMatchingInThePipeline:
    async def test_matches_are_written_before_the_alert_is_built(
        self, container: Container
    ) -> None:
        dispatcher = _CapturingDispatcher()
        result = await _run(container, dispatcher)

        assert result.partner_matches_written > 0
        session = container.session()
        rows = await PartnerMatchRepository(session).list_for_game(UNIVERSE)
        await session.close()
        assert rows
        assert all(row.scout_score is not None for row in rows)

    async def test_the_alert_payload_names_the_eligible_partners(
        self, container: Container
    ) -> None:
        dispatcher = _CapturingDispatcher()
        result = await _run(container, dispatcher)

        assert result.scores_computed == 1
        assert dispatcher.payloads, "a HOT lead should have produced an alert"
        payload = dispatcher.payloads[0]
        partners = payload["partners"]
        assert partners, "the alert should carry at least one partner"
        assert len(partners) <= MAX_ALERT_PARTNERS
        assert all(partner["eligible"] for partner in partners)
        assert all(partner["partner_name"] for partner in partners)

    async def test_a_rejected_game_is_never_matched(self, container: Container) -> None:
        dispatcher = _CapturingDispatcher()
        result = await _run(
            container,
            dispatcher,
            details=_details(current_ccu=0, lifetime_visits=0, name=None),
        )
        assert result.games_rejected == 1
        assert result.partner_matches_written == 0
        assert dispatcher.payloads == []

    async def test_matching_disabled_writes_nothing(self, settings: Settings) -> None:
        settings.partners.enabled = False
        instance = Container(settings)
        async with instance.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            result = await _run(instance, _CapturingDispatcher())
            assert result.partner_matches_written == 0
            session = instance.session()
            rows = await PartnerMatchRepository(session).list_for_game(UNIVERSE)
            await session.close()
            assert rows == []
        finally:
            await instance.engine.dispose()

    async def test_a_broken_matcher_does_not_break_the_scan(self, container: Container) -> None:
        """Partner matching is a nice-to-have; a failure must not lose the score."""

        class _Exploding:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def profiles(self, **kwargs: Any) -> Any:
                raise RuntimeError("profile file unreadable")

        dispatcher = _CapturingDispatcher()
        original = Container.partner_service

        def _boom(self: Container, session: AsyncSession) -> _Exploding:
            return _Exploding()

        Container.partner_service = _boom  # type: ignore[method-assign]
        try:
            result = await _run(container, dispatcher)
        finally:
            Container.partner_service = original  # type: ignore[method-assign]

        assert result.scores_computed == 1
        assert result.partner_matches_written == 0
        assert dispatcher.payloads
        assert dispatcher.payloads[0]["partners"] == []

    async def test_the_game_row_is_a_lead(self, container: Container) -> None:
        await _run(container, _CapturingDispatcher())
        session = container.session()
        game = await GameRepository(session).get(UNIVERSE)
        await session.close()
        assert game is not None
        assert game.current_score is not None
        assert game.is_lead
