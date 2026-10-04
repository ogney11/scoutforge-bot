"""Repository layer.

All SQL lives here as parameterized SQLAlchemy Core/ORM statements - no string
interpolation anywhere. Repositories take an ``AsyncSession``; the caller owns
the transaction (see :func:`scoutforge.database.session.session_scope`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    Select,
    delete,
    desc,
    false,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from scoutforge.config.logging import get_logger, log_event
from scoutforge.database.models import (
    Alert,
    DiscoveryRun,
    Game,
    GameScore,
    GameSnapshot,
    LeadNote,
    LeadPartner,
    LeadStatusHistory,
    PartnerMatch,
    ScoutUser,
    ServerStat,
    Setting,
)
from scoutforge.database.sql_compat import date_expression, dialect_name
from scoutforge.enums import AlertLevel, LeadStatus, PartnerOutreach, RunStatus
from scoutforge.utils import ensure_utc, utcnow

log = get_logger(__name__)


@dataclass(slots=True)
class UpsertResult:
    """Outcome of :meth:`GameRepository.upsert`."""

    game: Game
    created: bool
    metrics_changed: bool


class _BaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------
class GameRepository(_BaseRepository):
    """CRUD and query helpers for :class:`~scoutforge.database.models.Game`."""

    async def get(self, universe_id: int) -> Game | None:
        """Look a game up by its Roblox universe id.

        ``Game`` uses a surrogate ``id`` primary key, so ``session.get`` cannot
        be used here - it would look up the wrong row (or nothing at all).
        ``universe_id`` carries a UNIQUE constraint and an index, so this stays
        a single indexed lookup.
        """
        stmt = select(Game).where(Game.universe_id == universe_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(self, universe_ids: Sequence[int]) -> list[Game]:
        if not universe_ids:
            return []
        stmt = select(Game).where(Game.universe_id.in_(list(universe_ids)))
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        universe_id: int,
        data: dict[str, Any],
        *,
        source: str | None = None,
    ) -> UpsertResult:
        """Insert or update a game by ``universe_id``.

        Only fields present in ``data`` are written, so a partially available
        upstream payload never clobbers a previously known good value.
        """
        game = await self.get(universe_id)
        now = utcnow()

        if game is None:
            game = Game(
                universe_id=universe_id,
                first_seen_at=now,
                last_seen_at=now,
                **self._filter_fields(data),
            )
            game.peak_ccu = game.current_ccu or 0
            # Provenance has to be stamped on insert too: without it a game that
            # is never rediscovered by another source keeps no origin at all.
            game.discovery_source = source
            self.session.add(game)
            await self.session.flush()
            return UpsertResult(game=game, created=True, metrics_changed=True)

        before_ccu = game.current_ccu
        before_visits = game.lifetime_visits
        for key, value in self._filter_fields(data).items():
            if value is None and key in {
                "current_ccu",
                "lifetime_visits",
                "favorites",
                "likes",
                "dislikes",
                "thumbnail_url",
            }:
                # Never downgrade a known metric to "unknown".
                continue
            setattr(game, key, value)

        game.last_seen_at = now
        if game.current_ccu is not None:
            game.peak_ccu = max(game.peak_ccu or 0, game.current_ccu)
        if source and not game.discovery_source:
            game.discovery_source = source

        await self.session.flush()
        changed = before_ccu != game.current_ccu or before_visits != game.lifetime_visits
        return UpsertResult(game=game, created=False, metrics_changed=changed)

    @staticmethod
    def _filter_fields(data: dict[str, Any]) -> dict[str, Any]:
        allowed = {c.name for c in Game.__table__.columns}
        return {k: v for k, v in data.items() if k in allowed and k != "universe_id"}

    async def mark_lead(self, game: Game, *, lead_min_score: float = 0.0) -> None:
        """Promote a game to lead status when it clears the configured floor."""
        if game.is_lead:
            return
        if (game.current_score or 0.0) < lead_min_score:
            return
        game.is_lead = True
        game.lead_created_at = game.lead_created_at or utcnow()
        if game.status == LeadStatus.NEW:
            self.session.add(
                LeadStatusHistory(
                    universe_id=game.universe_id,
                    from_status=None,
                    to_status=LeadStatus.NEW,
                    reason="automatically promoted to lead by discovery worker",
                )
            )
        await self.session.flush()

    async def list_leads(
        self,
        *,
        status: LeadStatus | None = None,
        statuses: Sequence[LeadStatus] | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        level: AlertLevel | None = None,
        search: str | None = None,
        order_by: str = "score",
        limit: int = 25,
        offset: int = 0,
        leads_only: bool = True,
    ) -> list[Game]:
        stmt = select(Game)
        if leads_only:
            stmt = stmt.where(Game.is_lead.is_(True))
        if status is not None:
            stmt = stmt.where(Game.status == status.value)
        if statuses:
            stmt = stmt.where(Game.status.in_([s.value for s in statuses]))
        if min_score is not None:
            stmt = stmt.where(Game.current_score >= min_score)
        if max_score is not None:
            stmt = stmt.where(Game.current_score <= max_score)
        if level is not None:
            stmt = stmt.where(Game.current_level == level.value)
        if search:
            pattern = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Game.game_name).like(pattern),
                    func.lower(func.coalesce(Game.creator_name, "")).like(pattern),
                )
            )
        stmt = self._apply_order(stmt, order_by).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_leads(self, *, status: LeadStatus | None = None) -> int:
        stmt = select(func.count()).select_from(Game).where(Game.is_lead.is_(True))
        if status is not None:
            stmt = stmt.where(Game.status == status.value)
        return int((await self.session.execute(stmt)).scalar_one())

    async def status_counts(self) -> dict[str, int]:
        stmt = select(Game.status, func.count()).where(Game.is_lead.is_(True)).group_by(Game.status)
        return {
            str(status): int(count) for status, count in (await self.session.execute(stmt)).all()
        }

    async def level_counts(self) -> dict[str, int]:
        stmt = (
            select(Game.current_level, func.count())
            .where(Game.is_lead.is_(True), Game.current_level.isnot(None))
            .group_by(Game.current_level)
        )
        return {str(level): int(count) for level, count in (await self.session.execute(stmt)).all()}

    async def top_opportunities(self, limit: int = 10) -> list[Game]:
        stmt = (
            select(Game)
            .where(Game.is_lead.is_(True))
            .where(Game.status.notin_([LeadStatus.REJECTED.value, LeadStatus.NOT_INTERESTED.value]))
            .order_by(desc(Game.current_score), desc(Game.current_ccu))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def games_needing_refresh(
        self, universe_ids: Sequence[int]
    ) -> dict[int, datetime | None]:
        """Return last-seen timestamps for the given universes (staleness check)."""
        if not universe_ids:
            return {}
        stmt = select(Game.universe_id, Game.last_seen_at).where(
            Game.universe_id.in_(list(universe_ids))
        )
        return {uid: ensure_utc(seen) for uid, seen in (await self.session.execute(stmt)).all()}

    async def active_universe_ids(self, limit: int = 1000) -> list[int]:
        stmt = (
            select(Game.universe_id)
            .where(Game.is_lead.is_(True))
            .order_by(desc(Game.last_seen_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_score(self, game: Game, *, score: float, level: AlertLevel) -> None:
        """Denormalise the current score onto the game row for fast queries."""
        game.current_score = round(score, 2)
        game.current_level = level.value
        game.score_updated_at = utcnow()
        await self.session.flush()

    async def update_status(
        self,
        game: Game,
        new_status: LeadStatus,
        *,
        actor_id: int | None,
        actor_name: str | None,
        reason: str | None = None,
    ) -> LeadStatusHistory:
        """Change the CRM status and append to the audit trail."""
        previous = LeadStatus(game.status) if game.status else None
        game.status = new_status
        now = utcnow()
        if new_status == LeadStatus.CONTACTED and game.contacted_at is None:
            game.contacted_at = now
        if new_status in (LeadStatus.REPLIED, LeadStatus.INTERESTED) and game.responded_at is None:
            game.responded_at = now
        if new_status == LeadStatus.SENT_TO_WORLDENT and game.submitted_to_worldent_at is None:
            game.submitted_to_worldent_at = now

        entry = LeadStatusHistory(
            universe_id=game.universe_id,
            from_status=previous.value if previous else None,
            to_status=new_status.value,
            changed_by_id=actor_id,
            changed_by_name=actor_name,
            reason=reason,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def delete_missing(self, keep_universe_ids: Iterable[int]) -> int:
        """Remove non-lead games never seen for 90+ days. Used by maintenance."""
        cutoff = utcnow() - timedelta(days=90)
        stmt = delete(Game).where(
            Game.is_lead.is_(False),
            Game.last_seen_at < cutoff,
            Game.universe_id.notin_(list(keep_universe_ids)),
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        return int(result.rowcount or 0)

    @staticmethod
    def _apply_order(stmt: Select[Any], order_by: str) -> Select[Any]:
        mapping = {
            "score": desc(Game.current_score),
            "ccu": desc(Game.current_ccu),
            "visits": desc(Game.lifetime_visits),
            "newest": desc(Game.first_seen_at),
            "seen": desc(Game.last_seen_at),
            "name": Game.game_name.asc(),
        }
        return stmt.order_by(mapping.get(order_by, desc(Game.current_score)))

    async def counts_by_status_all(self) -> dict[str, int]:
        stmt = select(Game.status, func.count()).group_by(Game.status)
        return {str(s): int(c) for s, c in (await self.session.execute(stmt)).all()}


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------
class GameSnapshotRepository(_BaseRepository):
    """Time-series access to observed metrics."""

    async def add_many(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.session.add_all([GameSnapshot(**row) for row in rows])
        await self.session.flush()
        return len(rows)

    async def latest(
        self, universe_id: int, *, before: datetime | None = None
    ) -> GameSnapshot | None:
        stmt = select(GameSnapshot).where(GameSnapshot.universe_id == universe_id)
        if before is not None:
            stmt = stmt.where(GameSnapshot.captured_at <= before)
        stmt = stmt.order_by(desc(GameSnapshot.captured_at)).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def window(
        self,
        universe_id: int,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[GameSnapshot]:
        stmt = select(GameSnapshot).where(GameSnapshot.universe_id == universe_id)
        if since is not None:
            stmt = stmt.where(GameSnapshot.captured_at >= since)
        stmt = stmt.order_by(GameSnapshot.captured_at.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for(self, universe_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(GameSnapshot)
            .where(GameSnapshot.universe_id == universe_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def prune(self, retention_days: int) -> int:
        cutoff = utcnow() - timedelta(days=retention_days)
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                delete(GameSnapshot).where(GameSnapshot.captured_at < cutoff)
            ),
        )
        return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------
class GameScoreRepository(_BaseRepository):
    """Persistence and lookup of Scout Score history."""

    async def add(self, row: dict[str, Any]) -> GameScore:
        score = GameScore(**row)
        self.session.add(score)
        await self.session.flush()
        return score

    async def latest(self, universe_id: int) -> GameScore | None:
        stmt = (
            select(GameScore)
            .where(GameScore.universe_id == universe_id)
            .order_by(desc(GameScore.computed_at))
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def history(self, universe_id: int, limit: int = 100) -> list[GameScore]:
        stmt = (
            select(GameScore)
            .where(GameScore.universe_id == universe_id)
            .order_by(desc(GameScore.computed_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def prune(self, keep_per_game: int = 200) -> int:
        """Keep only the newest ``keep_per_game`` scores per universe."""
        if keep_per_game <= 0:
            return 0
        stmt = (
            select(GameScore.universe_id, func.count(GameScore.id).label("cnt"))
            .group_by(GameScore.universe_id)
            .having(func.count(GameScore.id) > keep_per_game)
        )
        heavy = [row[0] for row in (await self.session.execute(stmt)).all()]
        removed = 0
        for universe_id in heavy:
            keep_ids = (
                select(GameScore.id)
                .where(GameScore.universe_id == universe_id)
                .order_by(desc(GameScore.computed_at))
                .limit(keep_per_game)
            )
            result = cast(
                "CursorResult[Any]",
                await self.session.execute(
                    delete(GameScore).where(
                        GameScore.universe_id == universe_id, GameScore.id.notin_(keep_ids)
                    )
                ),
            )
            removed += int(result.rowcount or 0)
        return removed


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------
class LeadNoteRepository(_BaseRepository):
    async def add(
        self,
        universe_id: int,
        body: str,
        *,
        author_id: int | None = None,
        author_name: str | None = None,
    ) -> LeadNote:
        note = LeadNote(
            universe_id=universe_id,
            body=body,
            author_id=author_id,
            author_name=author_name,
        )
        self.session.add(note)
        await self.session.flush()
        return note

    async def list_for(self, universe_id: int, limit: int = 25) -> list[LeadNote]:
        stmt = (
            select(LeadNote)
            .where(LeadNote.universe_id == universe_id)
            .order_by(desc(LeadNote.created_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class LeadStatusHistoryRepository(_BaseRepository):
    async def add(self, entry: LeadStatusHistory) -> LeadStatusHistory:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_for(self, universe_id: int, limit: int = 50) -> list[LeadStatusHistory]:
        stmt = (
            select(LeadStatusHistory)
            .where(LeadStatusHistory.universe_id == universe_id)
            .order_by(desc(LeadStatusHistory.created_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Partner matching
# ---------------------------------------------------------------------------
class PartnerMatchRepository(_BaseRepository):
    """Latest partner verdict per game, plus cross-game partner leaderboards."""

    async def replace_for_game(
        self,
        universe_id: int,
        rows: Iterable[dict[str, Any]],
        *,
        scout_score: float | None = None,
        game_stage: str | None = None,
    ) -> int:
        """Overwrite every stored match for one game with a fresh matcher run.

        Matches are volatile - they change when the game's numbers change - so
        they are replaced rather than appended, which also keeps the
        ``(universe_id, partner_key)`` uniqueness meaningful.
        """
        wanted = [row for row in rows if row.get("partner_key")]
        wanted_keys = {str(row["partner_key"]) for row in wanted}

        if wanted_keys:
            await self.session.execute(
                delete(PartnerMatch).where(
                    PartnerMatch.universe_id == universe_id,
                    PartnerMatch.partner_key.notin_(wanted_keys),
                )
            )

        now = utcnow()
        for row in wanted:
            self.session.add(
                PartnerMatch(
                    universe_id=universe_id,
                    partner_key=str(row["partner_key"]),
                    partner_name=str(row.get("partner_name") or row["partner_key"]),
                    score=float(row.get("score") or 0.0),
                    eligible=bool(row.get("eligible")),
                    revenue_share=row.get("revenue_share"),
                    deal_terms=dict(row.get("deal_terms") or {}),
                    reasons=[str(reason) for reason in row.get("reasons") or []],
                    exclusions=[str(item) for item in row.get("mismatches") or []],
                    scout_score=scout_score,
                    game_stage=game_stage,
                    matched_at=now,
                )
            )
        await self.session.flush()
        return len(wanted)

    async def list_for_game(
        self, universe_id: int, *, eligible_only: bool = False
    ) -> list[PartnerMatch]:
        stmt = select(PartnerMatch).where(PartnerMatch.universe_id == universe_id)
        if eligible_only:
            stmt = stmt.where(PartnerMatch.eligible.is_(True))
        stmt = stmt.order_by(desc(PartnerMatch.score))
        return list((await self.session.execute(stmt)).scalars().all())

    async def top_for_partner(
        self, partner_key: str, *, limit: int = 25, eligible_only: bool = True
    ) -> list[PartnerMatch]:
        stmt = select(PartnerMatch).where(PartnerMatch.partner_key == partner_key)
        if eligible_only:
            stmt = stmt.where(PartnerMatch.eligible.is_(True))
        stmt = stmt.order_by(desc(PartnerMatch.score), desc(PartnerMatch.matched_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_partner(self) -> dict[str, int]:
        stmt = (
            select(PartnerMatch.partner_key, func.count(PartnerMatch.id))
            .where(PartnerMatch.eligible.is_(True))
            .group_by(PartnerMatch.partner_key)
        )
        return {str(key): int(count) for key, count in (await self.session.execute(stmt)).all()}

    async def eligible_universe_ids(
        self, partner_key: str | None = None, *, limit: int = 500
    ) -> list[int]:
        stmt = select(PartnerMatch.universe_id).where(PartnerMatch.eligible.is_(True))
        if partner_key:
            stmt = stmt.where(PartnerMatch.partner_key == partner_key)
        stmt = stmt.order_by(desc(PartnerMatch.score)).limit(limit)
        return [int(row[0]) for row in (await self.session.execute(stmt)).all()]

    async def delete_for_game(self, universe_id: int) -> int:
        rows = await self.list_for_game(universe_id)
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()
        return len(rows)


class LeadPartnerRepository(_BaseRepository):
    """Manual partner outreach. ScoutForge never sends the message itself."""

    async def get(self, universe_id: int, partner_key: str) -> LeadPartner | None:
        stmt = select(LeadPartner).where(
            LeadPartner.universe_id == universe_id, LeadPartner.partner_key == partner_key
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def upsert(
        self,
        universe_id: int,
        partner_key: str,
        *,
        partner_name: str = "",
        match_score: float | None = None,
        status: PartnerOutreach | str = PartnerOutreach.NEW,
        revenue_share_pct: float | None = None,
        deal_terms: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> LeadPartner:
        row = await self.get(universe_id, partner_key)
        status_value = status.value if isinstance(status, PartnerOutreach) else str(status)
        if row is None:
            row = LeadPartner(
                universe_id=universe_id,
                partner_key=partner_key,
                partner_name=partner_name or partner_key,
                created_at=utcnow(),
            )
            self.session.add(row)
        row.partner_name = partner_name or row.partner_name or partner_key
        row.status = status_value
        if match_score is not None:
            row.match_score = match_score
        if revenue_share_pct is not None:
            row.revenue_share_pct = revenue_share_pct
        if deal_terms is not None:
            row.deal_terms = deal_terms
        if notes is not None:
            row.notes = notes
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def record_contact(
        self,
        universe_id: int,
        partner_key: str,
        *,
        contact_method: str,
        contact_reference: str | None = None,
        author_id: int | None = None,
        author_name: str | None = None,
        partner_name: str = "",
        notes: str | None = None,
    ) -> LeadPartner:
        """Log that a human reached out, and who did it.

        ``contact_reference`` is the operator's own evidence - a Discord message
        link, an email thread id - so a claimed deal can always be audited.
        """
        row = await self.get(universe_id, partner_key)
        if row is None:
            row = await self.upsert(universe_id, partner_key, partner_name=partner_name)
        row.contacted_at = utcnow()
        row.contact_method = contact_method
        row.contact_reference = contact_reference
        row.contacted_by_id = author_id
        row.contacted_by_name = author_name
        if notes is not None:
            row.notes = notes
        if row.status in (PartnerOutreach.NEW.value, PartnerOutreach.SHORTLISTED.value):
            row.status = PartnerOutreach.CONTACTED.value
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def set_status(
        self,
        universe_id: int,
        partner_key: str,
        status: PartnerOutreach | str,
        *,
        notes: str | None = None,
        revenue_share_pct: float | None = None,
    ) -> LeadPartner:
        row = await self.get(universe_id, partner_key)
        if row is None:
            row = await self.upsert(universe_id, partner_key)
        row.status = status.value if isinstance(status, PartnerOutreach) else str(status)
        if notes is not None:
            row.notes = notes
        if revenue_share_pct is not None:
            row.revenue_share_pct = revenue_share_pct
        if row.status == PartnerOutreach.DEAL.value and row.responded_at is None:
            row.responded_at = utcnow()
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def list_for_game(self, universe_id: int) -> list[LeadPartner]:
        stmt = (
            select(LeadPartner)
            .where(LeadPartner.universe_id == universe_id)
            .order_by(desc(LeadPartner.match_score), desc(LeadPartner.updated_at))
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self) -> dict[str, int]:
        stmt = select(LeadPartner.status, func.count(LeadPartner.id)).group_by(LeadPartner.status)
        return {str(key): int(count) for key, count in (await self.session.execute(stmt)).all()}

    async def pipeline(self, *, limit: int = 100) -> list[LeadPartner]:
        stmt = select(LeadPartner).order_by(desc(LeadPartner.updated_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class AlertRepository(_BaseRepository):
    """Alert history powering the cooldown engine."""

    async def add(self, row: dict[str, Any]) -> Alert:
        alert = Alert(**row)
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def last_delivered(self, universe_id: int, levels: Sequence[AlertLevel]) -> Alert | None:
        if not levels:
            return None
        stmt = (
            select(Alert)
            .where(
                Alert.universe_id == universe_id,
                Alert.delivered.is_(True),
                Alert.level.in_([level.value for level in levels]),
            )
            .order_by(desc(Alert.created_at))
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def last_any_delivered(self, universe_id: int) -> Alert | None:
        stmt = (
            select(Alert)
            .where(Alert.universe_id == universe_id, Alert.delivered.is_(True))
            .order_by(desc(Alert.created_at))
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def recent(self, limit: int = 25) -> list[Alert]:
        stmt = select(Alert).order_by(desc(Alert.created_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def list_for_game(self, universe_id: int, limit: int = 25) -> list[Alert]:
        stmt = (
            select(Alert)
            .where(Alert.universe_id == universe_id)
            .order_by(desc(Alert.created_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_level(self, since: datetime | None = None) -> dict[str, int]:
        stmt = select(Alert.level, func.count()).where(Alert.delivered.is_(True))
        if since is not None:
            stmt = stmt.where(Alert.created_at >= since)
        stmt = stmt.group_by(Alert.level)
        return {str(level): int(count) for level, count in (await self.session.execute(stmt)).all()}


# ---------------------------------------------------------------------------
# Users & settings
# ---------------------------------------------------------------------------
class ScoutUserRepository(_BaseRepository):
    async def upsert(
        self,
        user_id: int,
        *,
        username: str | None = None,
        display_name: str | None = None,
        is_admin: bool | None = None,
    ) -> ScoutUser:
        user = await self.session.get(ScoutUser, user_id)
        if user is None:
            user = ScoutUser(id=user_id, username=username, display_name=display_name)
            if is_admin is not None:
                user.is_admin = is_admin
            self.session.add(user)
        else:
            if username:
                user.username = username
            if display_name:
                user.display_name = display_name
            if is_admin is not None:
                user.is_admin = is_admin
            user.last_seen_at = utcnow()
            user.active = True
        await self.session.flush()
        return user

    async def assign(self, universe_id: int, *, user_id: int, display_name: str) -> None:
        stmt = (
            update(Game)
            .where(Game.universe_id == universe_id)
            .values(assigned_scout_id=user_id, assigned_scout_name=display_name)
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def list_active(self) -> list[ScoutUser]:
        stmt = select(ScoutUser).where(ScoutUser.active.is_(True)).order_by(ScoutUser.display_name)
        return list((await self.session.execute(stmt)).scalars().all())


class SettingsRepository(_BaseRepository):
    """Key/value runtime configuration."""

    async def get(self, key: str, default: Any = None) -> Any:
        row = await self.session.get(Setting, key)
        return row.value if row else default

    async def set(
        self, key: str, value: Any, *, description: str | None = None, updated_by: int | None = None
    ) -> Setting:
        row = await self.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value, description=description, updated_by=updated_by)
            self.session.add(row)
        else:
            row.value = value
            if description is not None:
                row.description = description
            row.updated_by = updated_by
            row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def all(self) -> dict[str, Any]:
        stmt = select(Setting)
        return {row.key: row.value for row in (await self.session.execute(stmt)).scalars().all()}


# ---------------------------------------------------------------------------
# Runs & stats
# ---------------------------------------------------------------------------
class DiscoveryRunRepository(_BaseRepository):
    async def start(self, *, triggered_by: str) -> DiscoveryRun:
        run = DiscoveryRun(started_at=utcnow(), status=RunStatus.RUNNING, triggered_by=triggered_by)
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish(
        self, run: DiscoveryRun, *, status: RunStatus, error: str | None = None
    ) -> DiscoveryRun:
        run.status = status
        run.finished_at = utcnow()
        duration = (run.finished_at - run.started_at).total_seconds()
        run.duration_ms = int(duration * 1000)
        run.error = error
        await self.session.flush()
        log_event(
            log,
            20 if status == RunStatus.SUCCESS else 25,
            "SCAN_FINISHED",
            run_id=run.id,
            status=status.value,
            duration_ms=run.duration_ms,
            candidates=run.candidates_found,
            accepted=run.games_accepted,
            rejected=run.games_rejected,
        )
        return run

    async def recent(self, limit: int = 20) -> list[DiscoveryRun]:
        stmt = select(DiscoveryRun).order_by(desc(DiscoveryRun.started_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def summary(self, since: datetime | None = None) -> dict[str, Any]:
        base = select(func.count(DiscoveryRun.id))
        if since is not None:
            base = base.where(DiscoveryRun.started_at >= since)
        runs = int((await self.session.execute(base)).scalar_one())

        agg = select(
            func.coalesce(func.sum(DiscoveryRun.candidates_found), 0),
            func.coalesce(func.sum(DiscoveryRun.games_discovered), 0),
            func.coalesce(func.sum(DiscoveryRun.games_accepted), 0),
            func.coalesce(func.sum(DiscoveryRun.games_rejected), 0),
            func.coalesce(func.sum(DiscoveryRun.snapshots_written), 0),
            func.coalesce(func.sum(DiscoveryRun.alerts_sent), 0),
            func.coalesce(func.sum(DiscoveryRun.alerts_suppressed), 0),
            func.coalesce(func.sum(DiscoveryRun.api_errors), 0),
            func.coalesce(func.avg(DiscoveryRun.duration_ms), 0.0),
        )
        if since is not None:
            agg = agg.where(DiscoveryRun.started_at >= since)
        row = (await self.session.execute(agg)).one()
        return {
            "scans_completed": runs,
            "candidates_found": int(row[0]),
            "games_discovered": int(row[1]),
            "games_accepted": int(row[2]),
            "games_rejected": int(row[3]),
            "snapshots_written": int(row[4]),
            "alerts_sent": int(row[5]),
            "alerts_suppressed": int(row[6]),
            "api_errors": int(row[7]),
            "average_scan_duration_ms": round(float(row[8]), 2),
        }

    async def is_running(self) -> bool:
        stmt = select(func.count(DiscoveryRun.id)).where(
            DiscoveryRun.status == RunStatus.RUNNING.value
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0

    async def recover_stale(self, older_than_seconds: int = 3600) -> int:
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                update(DiscoveryRun)
                .where(
                    DiscoveryRun.status == RunStatus.RUNNING.value, DiscoveryRun.started_at < cutoff
                )
                .values(
                    status=RunStatus.FAILED.value,
                    finished_at=utcnow(),
                    error="abandoned run recovered",
                )
            ),
        )
        return int(result.rowcount or 0)


class StatsRepository(_BaseRepository):
    """Simple counters plus dashboard aggregates."""

    async def increment(self, key: str, amount: float = 1.0, *, by: int | None = None) -> None:
        stmt = (
            update(ServerStat)
            .where(ServerStat.key == key)
            .values(value=ServerStat.value + amount, updated_at=utcnow(), updated_by=by)
            .returning(ServerStat.key)
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            self.session.add(ServerStat(key=key, value=amount, updated_at=utcnow(), updated_by=by))
        await self.session.flush()

    async def get(self, key: str, default: float = 0.0) -> float:
        row = await self.session.get(ServerStat, key)
        return float(row.value) if row else default

    async def snapshot(self) -> dict[str, float]:
        stmt = select(ServerStat)
        return {
            row.key: float(row.value) for row in (await self.session.execute(stmt)).scalars().all()
        }

    async def total_games(self) -> int:
        stmt = select(func.count()).select_from(Game)
        return int((await self.session.execute(stmt)).scalar_one())

    async def total_snapshots(self) -> int:
        stmt = select(func.count()).select_from(GameSnapshot)
        return int((await self.session.execute(stmt)).scalar_one())

    async def discoveries_per_day(self, days: int = 14) -> list[tuple[str, int]]:
        """Daily count of newly discovered universes, zero-filled across the window.

        Days without discoveries are returned as 0 rather than omitted so that
        time-series charts keep a continuous x-axis instead of silently
        stretching the gaps.
        """
        since = utcnow() - timedelta(days=days)
        day_expr = date_expression(Game.first_seen_at, dialect_name(self.session))
        stmt = (
            select(day_expr.label("day"), func.count(Game.universe_id))
            .where(Game.first_seen_at >= since)
            .group_by(day_expr)
            .order_by(day_expr)
        )
        counted = {str(row[0]): int(row[1]) for row in (await self.session.execute(stmt)).all()}
        start = since.date()
        today = utcnow().date()
        filled: list[tuple[str, int]] = []
        cursor = start
        while cursor <= today:
            key = cursor.isoformat()
            filled.append((key, counted.get(key, 0)))
            cursor += timedelta(days=1)
        return filled

    async def top_growing(self, limit: int = 10) -> list[Game]:
        """Leads ordered by CCU (a cheap, always-available growth proxy)."""
        stmt = (
            select(Game)
            .where(Game.is_lead.is_(True), Game.current_ccu.isnot(None))
            .order_by(desc(Game.current_ccu))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def games_with_scores_joined(self, universe_ids: Sequence[int]) -> list[Game]:
        if not universe_ids:
            return []
        stmt = (
            select(Game)
            .options(selectinload(Game.scores))
            .where(Game.universe_id.in_(list(universe_ids)))
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())


async def run_with_retry(
    session: AsyncSession,
    operation: str,
    coro_factory: Any,
    *,
    retries: int = 3,
    base_delay: float = 0.5,
) -> Any:
    """Retry a session operation on serialization / deadlock errors."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    delay = base_delay
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro_factory()
        except (OperationalError, DBAPIError) as exc:  # pragma: no cover - defensive
            last_error = exc
            await session.rollback()
            if attempt >= retries:
                break
            log_event(
                log, 25, "DB_RETRY", operation=operation, attempt=attempt, error=type(exc).__name__
            )
            await asyncio.sleep(delay)
            delay *= 2
    raise last_error  # type: ignore[misc]


def build_universe_filter(universe_ids: Sequence[int]) -> Any:
    """Helper building an ``IN`` predicate for the given universes."""
    return Game.universe_id.in_(list(universe_ids)) if universe_ids else false()


__all__ = [
    "AlertRepository",
    "DiscoveryRunRepository",
    "GameRepository",
    "GameScoreRepository",
    "GameSnapshotRepository",
    "LeadNoteRepository",
    "LeadPartnerRepository",
    "LeadStatusHistoryRepository",
    "PartnerMatchRepository",
    "ScoutUserRepository",
    "SettingsRepository",
    "StatsRepository",
    "UpsertResult",
    "build_universe_filter",
    "run_with_retry",
]
