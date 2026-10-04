"""Repository integration tests against a real schema.

These run on SQLite carrying the production metadata, so they exercise the real
SQLAlchemy statements rather than mocks. ``utcnow()`` inside the repository layer
is pinned to :data:`NOW` so retention cutoffs and daily buckets are deterministic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scoutforge.database import repositories
from scoutforge.database.models import DiscoveryRun
from scoutforge.database.repositories import (
    AlertRepository,
    DiscoveryRunRepository,
    GameRepository,
    GameSnapshotRepository,
    LeadStatusHistoryRepository,
    StatsRepository,
)
from scoutforge.enums import AlertLevel, LeadStatus, RunStatus
from scoutforge.tests.conftest import NOW

UNIVERSE = 900_000_001

GAME_PAYLOAD: dict[str, Any] = {
    "place_id": 1_234_567_890,
    "game_name": "Integration Game",
    "creator_id": 42,
    "creator_name": "TestDev",
    "creator_type": "User",
    "current_ccu": 5_000,
    "lifetime_visits": 120_000,
    "favorites": 9_000,
    "likes": 3_000,
    "dislikes": 120,
    "description": "A game",
    "thumbnail_url": "https://tr.rbxcdn.test/thumb.png",
    "max_players": 100,
    "playing_now": 5_000,
}


@pytest.fixture(autouse=True)
def _frozen_repository_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the repository layer's clock so prune/bucket maths is exact."""
    monkeypatch.setattr(repositories, "utcnow", lambda: NOW)


async def _make_lead(
    session: AsyncSession,
    universe_id: int,
    *,
    score: float = 90.0,
    level: AlertLevel = AlertLevel.HOT,
    **overrides: Any,
):
    repo = GameRepository(session)
    result = await repo.upsert(universe_id, {**GAME_PAYLOAD, **overrides}, source="test")
    await repo.set_score(result.game, score=score, level=level)
    await repo.mark_lead(result.game)
    return result.game


class TestGameUpsert:
    async def test_creates_a_new_row(self, session: AsyncSession) -> None:
        result = await GameRepository(session).upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        assert result.created is True
        assert result.metrics_changed is True
        assert result.game.universe_id == UNIVERSE
        assert result.game.game_name == "Integration Game"

    async def test_get_finds_the_row_by_universe_id(self, session: AsyncSession) -> None:
        """Regression: ``get()`` once queried the surrogate PK with a universe id."""
        await GameRepository(session).upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        assert await GameRepository(session).get(UNIVERSE) is not None

    async def test_get_missing_returns_none(self, session: AsyncSession) -> None:
        assert await GameRepository(session).get(123) is None

    async def test_second_upsert_updates_rather_than_duplicates(
        self, session: AsyncSession
    ) -> None:
        repo = GameRepository(session)
        first = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        second = await repo.upsert(
            UNIVERSE,
            {**GAME_PAYLOAD, "current_ccu": 7_500, "game_name": "Renamed"},
            source="test",
        )
        assert second.created is False
        assert second.metrics_changed is True
        assert second.game.current_ccu == 7_500
        assert second.game.game_name == "Renamed"
        assert first.game.id == second.game.id, "must update, never insert a second row"

    async def test_unchanged_metrics_report_no_change(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        assert (
            await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        ).metrics_changed is False

    async def test_null_metrics_stay_null_and_never_downgrade(self, session: AsyncSession) -> None:
        """Unknown upstream values must remain ``None`` and must not clobber good data."""
        repo = GameRepository(session)
        result = await repo.upsert(
            UNIVERSE, {**GAME_PAYLOAD, "favorites": None, "likes": None}, source="test"
        )
        assert result.game.favorites is None
        assert result.game.likes is None

        await repo.upsert(UNIVERSE, {**GAME_PAYLOAD, "current_ccu": None}, source="test")
        assert (await repo.get(UNIVERSE)).current_ccu == 5_000, "known CCU must not become None"

    async def test_peak_ccu_tracks_the_high_water_mark(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await repo.upsert(UNIVERSE, {**GAME_PAYLOAD, "current_ccu": 5_000}, source="test")
        await repo.upsert(UNIVERSE, {**GAME_PAYLOAD, "current_ccu": 2_000}, source="test")
        assert (await repo.get(UNIVERSE)).peak_ccu == 5_000

    async def test_partial_payload_does_not_clobber_other_columns(
        self, session: AsyncSession
    ) -> None:
        repo = GameRepository(session)
        await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.upsert(UNIVERSE, {"current_ccu": 6_000}, source="test")

        game = await repo.get(UNIVERSE)
        assert game.game_name == "Integration Game"
        assert game.creator_name == "TestDev"
        assert game.current_ccu == 6_000

    async def test_source_is_stamped_on_insert(self, session: AsyncSession) -> None:
        """Regression: new games used to keep ``discovery_source = None`` forever."""
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="roblox_search")
        assert result.game.discovery_source == "roblox_search"

        await repo.upsert(UNIVERSE, {"current_ccu": 6_000}, source="roblox_explore")
        assert (await repo.get(UNIVERSE)).discovery_source == "roblox_search", (
            "the original discovering source is provenance and must not be overwritten"
        )

    async def test_insert_without_a_source_leaves_it_null(self, session: AsyncSession) -> None:
        result = await GameRepository(session).upsert(UNIVERSE, dict(GAME_PAYLOAD))
        assert result.game.discovery_source is None

    async def test_first_seen_at_is_stamped_once(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        original = (await repo.get(UNIVERSE)).first_seen_at
        await repo.upsert(UNIVERSE, {**GAME_PAYLOAD, "current_ccu": 9_000}, source="test")
        assert (await repo.get(UNIVERSE)).first_seen_at == original


class TestScoringPersistence:
    async def test_set_score_denormalises_onto_the_game(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.set_score(result.game, score=93.456, level=AlertLevel.HOT)
        await session.commit()

        reloaded = await repo.get(UNIVERSE)
        assert reloaded.current_score == 93.46
        assert reloaded.current_level == AlertLevel.HOT.value

    async def test_level_reloads_as_a_plain_string(self, session: AsyncSession) -> None:
        """Enum columns are ``String`` on purpose; consumers must not call ``.value``."""
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.set_score(result.game, score=99.0, level=AlertLevel.EXTREME)
        await session.commit()

        reloaded = await repo.get(UNIVERSE)
        assert reloaded.current_level == "EXTREME"
        assert isinstance(reloaded.current_level, str)


class TestSnapshots:
    def _rows(self, count: int = 5, *, universe_id: int = UNIVERSE) -> list[dict[str, Any]]:
        return [
            {
                "universe_id": universe_id,
                "captured_at": NOW - timedelta(hours=count - index),
                "current_ccu": 1_000 + index * 100,
                "lifetime_visits": 40_000 + index * 4_000,
                "favorites": 500 + index * 50,
                "source": "test",
            }
            for index in range(count)
        ]

    async def test_add_many_persists_every_row(self, session: AsyncSession) -> None:
        assert await GameSnapshotRepository(session).add_many(self._rows()) == 5
        await session.commit()

    async def test_add_many_with_no_rows(self, session: AsyncSession) -> None:
        assert await GameSnapshotRepository(session).add_many([]) == 0

    async def test_window_returns_oldest_first(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        rows = await repo.window(UNIVERSE)
        assert [row.current_ccu for row in rows] == [1_000, 1_100, 1_200, 1_300, 1_400]

    async def test_window_since_filters(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        assert len(await repo.window(UNIVERSE, since=NOW - timedelta(hours=2))) == 2

    async def test_window_limit_keeps_the_oldest_within_the_window(
        self, session: AsyncSession
    ) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        rows = await repo.window(UNIVERSE, limit=2)
        assert [row.current_ccu for row in rows] == [1_000, 1_100]

    async def test_latest_returns_the_newest(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        latest = await repo.latest(UNIVERSE)
        assert latest is not None
        assert latest.current_ccu == 1_400

    async def test_latest_before_excludes_newer_points(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        latest = await repo.latest(UNIVERSE, before=NOW - timedelta(hours=2))
        assert latest.current_ccu == 1_300

    async def test_latest_without_history_is_none(self, session: AsyncSession) -> None:
        assert await GameSnapshotRepository(session).latest(UNIVERSE) is None

    async def test_snapshots_are_isolated_per_universe(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many([*self._rows(), *self._rows(1, universe_id=UNIVERSE + 1)])
        await session.commit()

        assert len(await repo.window(UNIVERSE)) == 5
        assert len(await repo.window(UNIVERSE + 1)) == 1

    async def test_count_for(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(self._rows())
        await session.commit()

        assert await repo.count_for(UNIVERSE) == 5
        assert await repo.count_for(UNIVERSE + 99) == 0

    async def test_prune_drops_points_older_than_the_retention_window(
        self, session: AsyncSession
    ) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(
            [
                {**row, "captured_at": NOW - timedelta(days=10 - index)}
                for index, row in enumerate(self._rows(10))
            ]
        )
        await session.commit()

        # cutoff is exclusive: days 4..10 go, days 1..3 stay.
        assert await repo.prune(3) == 7
        assert len(await repo.window(UNIVERSE)) == 3

    async def test_prune_keeps_everything_when_nothing_expired(self, session: AsyncSession) -> None:
        repo = GameSnapshotRepository(session)
        await repo.add_many(
            [
                {**row, "captured_at": NOW - timedelta(days=index)}
                for index, row in enumerate(self._rows(5))
            ]
        )
        await session.commit()

        assert await repo.prune(90) == 0


class TestLeadQueries:
    async def test_list_leads_returns_only_leads(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE)
        await repo.upsert(UNIVERSE + 1, {**GAME_PAYLOAD, "game_name": "Not A Lead"}, source="test")
        await session.commit()

        assert [game.universe_id for game in await repo.list_leads()] == [UNIVERSE]

    async def test_leads_only_false_includes_everything(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE)
        await repo.upsert(UNIVERSE + 1, {**GAME_PAYLOAD, "game_name": "Not A Lead"}, source="test")
        await session.commit()

        assert len(await repo.list_leads(leads_only=False)) == 2

    async def test_filter_by_status(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE)
        second = await _make_lead(session, UNIVERSE + 1)
        await repo.update_status(second, LeadStatus.CONTACTED, actor_id=1, actor_name="scout")
        await session.commit()

        contacted = await repo.list_leads(status=LeadStatus.CONTACTED)
        assert [game.universe_id for game in contacted] == [UNIVERSE + 1]
        fresh = await repo.list_leads(status=LeadStatus.NEW)
        assert [game.universe_id for game in fresh] == [UNIVERSE]

    async def test_filter_by_multiple_statuses(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        first = await _make_lead(session, UNIVERSE)
        second = await _make_lead(session, UNIVERSE + 1)
        third = await _make_lead(session, UNIVERSE + 2)
        await repo.update_status(second, LeadStatus.CONTACTED, actor_id=1, actor_name="scout")
        await repo.update_status(third, LeadStatus.INTERESTED, actor_id=1, actor_name="scout")
        await session.commit()
        assert first is not None

        rows = await repo.list_leads(statuses=[LeadStatus.CONTACTED, LeadStatus.INTERESTED])
        assert {game.universe_id for game in rows} == {UNIVERSE + 1, UNIVERSE + 2}

    async def test_filter_by_score_range(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE, score=95.0)
        await _make_lead(session, UNIVERSE + 1, score=80.0)

        assert [g.universe_id for g in await repo.list_leads(min_score=90.0)] == [UNIVERSE]
        assert [g.universe_id for g in await repo.list_leads(max_score=85.0)] == [UNIVERSE + 1]

    async def test_filter_by_level(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE, level=AlertLevel.EXTREME)
        await _make_lead(session, UNIVERSE + 1, level=AlertLevel.WARM)

        rows = await repo.list_leads(level=AlertLevel.EXTREME)
        assert [game.universe_id for game in rows] == [UNIVERSE]

    async def test_search_matches_name_and_creator_case_insensitively(
        self, session: AsyncSession
    ) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE, game_name="Bloxburg Reborn")
        await _make_lead(session, UNIVERSE + 1, game_name="Adopt Me", creator_name="Uplift Games")

        assert [g.universe_id for g in await repo.list_leads(search="BLOXBURG")] == [UNIVERSE]
        assert [g.universe_id for g in await repo.list_leads(search="uplift")] == [UNIVERSE + 1]
        assert await repo.list_leads(search="nothing-matches") == []

    @pytest.mark.parametrize(
        ("order_by", "expected"),
        [
            ("score", [3, 1, 2]),
            ("ccu", [2, 3, 1]),
            ("name", [3, 1, 2]),
        ],
    )
    async def test_order_by(
        self, session: AsyncSession, order_by: str, expected: list[int]
    ) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE, score=90.0, current_ccu=5_000, game_name="Bravo")
        await _make_lead(session, UNIVERSE + 1, score=80.0, current_ccu=9_000, game_name="Charlie")
        await _make_lead(session, UNIVERSE + 2, score=99.0, current_ccu=7_000, game_name="Alpha")

        rows = await repo.list_leads(order_by=order_by)
        assert [game.universe_id - UNIVERSE + 1 for game in rows] == expected

    async def test_limit_and_offset_page_through_results(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        for index in range(5):
            await _make_lead(session, UNIVERSE + index, score=80.0 + index)

        first = await repo.list_leads(limit=2)
        second = await repo.list_leads(limit=2, offset=2)
        assert len(first) == 2
        assert len(second) == 2
        assert {g.universe_id for g in first}.isdisjoint({g.universe_id for g in second})

    async def test_count_leads(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE)
        await _make_lead(session, UNIVERSE + 1)
        await session.commit()

        assert await repo.count_leads() == 2

    async def test_counts_by_status_all(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        first = await _make_lead(session, UNIVERSE)
        second = await _make_lead(session, UNIVERSE + 1)
        await repo.update_status(second, LeadStatus.CONTACTED, actor_id=1, actor_name="scout")
        await session.commit()
        assert first is not None

        counts = await repo.counts_by_status_all()
        assert counts[LeadStatus.NEW.value] == 1
        assert counts[LeadStatus.CONTACTED.value] == 1

    async def test_top_opportunities_ranks_by_score(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await _make_lead(session, UNIVERSE, score=80.0)
        await _make_lead(session, UNIVERSE + 1, score=99.0)

        assert [g.universe_id for g in await repo.top_opportunities(limit=1)] == [UNIVERSE + 1]

    async def test_mark_lead_records_the_promotion_in_the_audit_trail(
        self, session: AsyncSession
    ) -> None:
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.mark_lead(result.game)
        await session.commit()

        assert result.game.is_lead is True
        assert result.game.lead_created_at is not None

        trail = await LeadStatusHistoryRepository(session).list_for(UNIVERSE)
        assert [row.to_status for row in trail] == [LeadStatus.NEW.value]

    async def test_mark_lead_is_idempotent(self, session: AsyncSession) -> None:
        """Re-promoting must not append a duplicate NEW history row."""
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.mark_lead(result.game)
        original = result.game.lead_created_at
        await repo.mark_lead(result.game)
        await session.commit()

        assert result.game.lead_created_at == original
        assert len(await LeadStatusHistoryRepository(session).list_for(UNIVERSE)) == 1

    async def test_mark_lead_respects_the_score_floor(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.set_score(result.game, score=40.0, level=AlertLevel.NORMAL)
        await repo.mark_lead(result.game, lead_min_score=75.0)
        await session.commit()

        assert result.game.is_lead is False


class TestStatusTransitions:
    async def test_transition_is_recorded_with_actor_and_reason(
        self, session: AsyncSession
    ) -> None:
        repo = GameRepository(session)
        game = await _make_lead(session, UNIVERSE)
        entry = await repo.update_status(
            game, LeadStatus.CONTACTED, actor_id=42, actor_name="scout", reason="DM sent"
        )
        await session.commit()

        assert entry.from_status == LeadStatus.NEW.value
        assert entry.to_status == LeadStatus.CONTACTED.value
        assert entry.changed_by_id == 42
        assert entry.reason == "DM sent"
        assert (await repo.get(UNIVERSE)).status == LeadStatus.CONTACTED.value

    @pytest.mark.parametrize(
        ("status", "field"),
        [
            (LeadStatus.CONTACTED, "contacted_at"),
            (LeadStatus.REPLIED, "responded_at"),
            (LeadStatus.INTERESTED, "responded_at"),
            (LeadStatus.SENT_TO_WORLDENT, "submitted_to_worldent_at"),
        ],
    )
    async def test_milestone_timestamps_are_stamped_once(
        self, session: AsyncSession, status: LeadStatus, field: str
    ) -> None:
        repo = GameRepository(session)
        game = await _make_lead(session, UNIVERSE)
        await repo.update_status(game, status, actor_id=1, actor_name="scout")
        first = getattr(game, field)
        assert first is not None

        await repo.update_status(game, LeadStatus.NEW, actor_id=1, actor_name="scout")
        assert getattr(game, field) == first, f"{field} must not be overwritten"

    async def test_chained_transitions_keep_full_history(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        game = await _make_lead(session, UNIVERSE)
        for status in (LeadStatus.RESEARCHED, LeadStatus.CONTACTED, LeadStatus.REPLIED):
            await repo.update_status(game, status, actor_id=1, actor_name="scout")
        await session.commit()

        trail = await LeadStatusHistoryRepository(session).list_for(UNIVERSE)
        to_statuses = [row.to_status for row in trail]
        assert LeadStatus.REPLIED.value in to_statuses
        assert LeadStatus.CONTACTED.value in to_statuses


class TestAlerts:
    def _alert(
        self,
        *,
        hours_ago: float,
        level: AlertLevel = AlertLevel.HOT,
        delivered: bool = True,
        universe_id: int = UNIVERSE,
    ) -> dict[str, Any]:
        return {
            "universe_id": universe_id,
            "level": level.value,
            "score": 92.0,
            "ccu": 5_000,
            "delivered": delivered,
            "created_at": NOW - timedelta(hours=hours_ago),
        }

    async def test_last_delivered_ignores_suppressed_alerts(self, session: AsyncSession) -> None:
        """Regression: a suppressed alert must not start or reset a cooldown."""
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=6, level=AlertLevel.HOT, delivered=True))
        await repo.add(self._alert(hours_ago=1, level=AlertLevel.EXTREME, delivered=False))
        await session.commit()

        last = await repo.last_delivered(UNIVERSE, [AlertLevel.HOT, AlertLevel.EXTREME])
        assert last is not None
        assert last.level == AlertLevel.HOT.value

    async def test_last_delivered_without_history(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        assert await repo.last_delivered(UNIVERSE, [AlertLevel.HOT]) is None

    async def test_last_delivered_with_empty_level_list(self, session: AsyncSession) -> None:
        assert await AlertRepository(session).last_delivered(UNIVERSE, []) is None

    async def test_last_any_delivered(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=2, level=AlertLevel.WARM))
        await session.commit()

        last = await repo.last_any_delivered(UNIVERSE)
        assert last is not None
        assert last.level == AlertLevel.WARM.value

    async def test_counts_by_level_only_counts_delivered(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=1))
        await repo.add(self._alert(hours_ago=2))
        await repo.add(self._alert(hours_ago=3, level=AlertLevel.EXTREME))
        await repo.add(self._alert(hours_ago=4, level=AlertLevel.WARM, delivered=False))
        await session.commit()

        counts = await repo.counts_by_level()
        assert counts[AlertLevel.HOT.value] == 2
        assert counts[AlertLevel.EXTREME.value] == 1
        assert AlertLevel.WARM.value not in counts

    async def test_counts_by_level_respects_since(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=1))
        await repo.add(self._alert(hours_ago=100))
        await session.commit()

        assert await repo.counts_by_level(since=NOW - timedelta(hours=24)) == {
            AlertLevel.HOT.value: 1
        }

    async def test_list_for_game_is_newest_first(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=5))
        await repo.add(self._alert(hours_ago=1, level=AlertLevel.EXTREME))
        await session.commit()

        rows = await repo.list_for_game(UNIVERSE)
        assert [row.level for row in rows] == [AlertLevel.EXTREME.value, AlertLevel.HOT.value]

    async def test_list_for_game_respects_limit(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        for hours in range(1, 6):
            await repo.add(self._alert(hours_ago=hours))
        await session.commit()

        assert len(await repo.list_for_game(UNIVERSE, limit=2)) == 2

    async def test_recent_spans_games(self, session: AsyncSession) -> None:
        repo = AlertRepository(session)
        await repo.add(self._alert(hours_ago=1, universe_id=UNIVERSE))
        await repo.add(self._alert(hours_ago=2, universe_id=UNIVERSE + 1))
        await session.commit()

        assert len(await repo.recent()) == 2


class TestDiscoveryRuns:
    async def test_start_records_a_running_row(self, session: AsyncSession) -> None:
        run = await DiscoveryRunRepository(session).start(triggered_by="worker")
        assert run.status == RunStatus.RUNNING.value
        assert run.triggered_by == "worker"

    async def test_is_running_reflects_state(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        run = await repo.start(triggered_by="worker")
        await session.commit()
        assert await repo.is_running() is True

        await repo.finish(run, status=RunStatus.SUCCESS)
        await session.commit()
        assert await repo.is_running() is False

    async def test_finish_computes_duration_ms(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        run = await repo.start(triggered_by="worker")
        await repo.finish(run, status=RunStatus.SUCCESS)
        await session.commit()

        assert run.duration_ms is not None
        assert run.duration_ms >= 0
        assert run.finished_at is not None
        assert run.error is None

    async def test_finish_records_the_error(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        run = await repo.start(triggered_by="worker")
        await repo.finish(run, status=RunStatus.FAILED, error="boom")
        await session.commit()

        assert run.error == "boom"

    async def test_recover_stale_marks_abandoned_runs_failed(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        run = await repo.start(triggered_by="worker")
        run.started_at = NOW - timedelta(hours=5)
        await session.commit()

        assert await repo.recover_stale(older_than_seconds=3600) == 1
        await session.commit()
        reloaded = (
            await session.execute(select(DiscoveryRun).where(DiscoveryRun.id == run.id))
        ).scalar_one()
        assert reloaded.status == RunStatus.FAILED.value

    async def test_fresh_runs_are_untouched(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        await repo.start(triggered_by="worker")
        await session.commit()

        assert await repo.recover_stale(older_than_seconds=3600) == 0

    async def test_summary_aggregates_counters(self, session: AsyncSession) -> None:
        repo = DiscoveryRunRepository(session)
        first = await repo.start(triggered_by="worker")
        first.candidates_found = 100
        first.games_accepted = 3
        await repo.finish(first, status=RunStatus.SUCCESS)
        second = await repo.start(triggered_by="cron")
        second.candidates_found = 50
        await repo.finish(second, status=RunStatus.FAILED, error="boom")
        await session.commit()

        summary = await repo.summary()
        assert summary["scans_completed"] == 2
        assert summary["candidates_found"] == 150
        assert summary["games_accepted"] == 3

    async def test_summary_is_zeroed_not_absent_when_empty(self, session: AsyncSession) -> None:
        summary = await DiscoveryRunRepository(session).summary()
        assert summary["scans_completed"] == 0
        assert summary["candidates_found"] == 0
        assert summary["average_scan_duration_ms"] == 0.0


class TestStatsRepository:
    async def test_discoveries_per_day_is_zero_filled(self, session: AsyncSession) -> None:
        """Charts need a dense series; gaps must read as 0 rather than vanish."""
        repo = GameRepository(session)
        result = await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        result.game.first_seen_at = NOW - timedelta(days=3)
        await session.commit()

        counts = await StatsRepository(session).discoveries_per_day(days=4)
        assert len(counts) == 5, "today plus the previous four days"
        assert sum(count for _, count in counts) == 1
        assert all(isinstance(day, str) for day, _ in counts)

    async def test_discoveries_per_day_is_empty_without_games(self, session: AsyncSession) -> None:
        counts = await StatsRepository(session).discoveries_per_day(days=2)
        assert counts == [(day, 0) for day, _ in counts] and all(c == 0 for _, c in counts)

    async def test_counter_round_trip(self, session: AsyncSession) -> None:
        repo = StatsRepository(session)
        await repo.increment("scans_total")
        await repo.increment("scans_total")
        await repo.increment("alerts_total", 3)
        await session.commit()

        assert await repo.get("scans_total") == 2
        assert await repo.get("alerts_total") == 3
        assert await repo.get("missing") == 0.0
        assert await repo.get("missing", 7.0) == 7.0

    async def test_totals_count_rows(self, session: AsyncSession) -> None:
        repo = GameRepository(session)
        await repo.upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
        await repo.upsert(UNIVERSE + 1, {**GAME_PAYLOAD, "game_name": "Second"}, source="test")
        await session.commit()

        stats = StatsRepository(session)
        assert await stats.total_games() == 2
        assert await stats.total_snapshots() == 0


class TestSettingsRepository:
    async def test_set_then_get_round_trips(self, session: AsyncSession) -> None:
        from scoutforge.database.repositories import SettingsRepository

        repo = SettingsRepository(session)
        await repo.set("alerts_enabled", False)
        await session.commit()

        assert await repo.get("alerts_enabled") is False

    async def test_missing_key_returns_default(self, session: AsyncSession) -> None:
        from scoutforge.database.repositories import SettingsRepository

        assert await SettingsRepository(session).get("nope", "fallback") == "fallback"

    async def test_all_returns_every_stored_key(self, session: AsyncSession) -> None:
        from scoutforge.database.repositories import SettingsRepository

        repo = SettingsRepository(session)
        await repo.set("a", 1)
        await repo.set("b", 2)
        await session.commit()

        assert await repo.all() == {"a": 1, "b": 2}


class TestCommitVisibility:
    async def test_data_is_visible_to_a_fresh_session(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as first:
            await GameRepository(first).upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
            await first.commit()
        async with session_factory() as second:
            game = await GameRepository(second).get(UNIVERSE)
            assert game is not None
            assert game.game_name == "Integration Game"

    async def test_rollback_discards_writes(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as sess:
            await GameRepository(sess).upsert(UNIVERSE, dict(GAME_PAYLOAD), source="test")
            await sess.rollback()
        async with session_factory() as sess:
            assert await GameRepository(sess).get(UNIVERSE) is None
