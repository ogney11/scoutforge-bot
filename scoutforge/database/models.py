"""SQLAlchemy 2.x declarative models.

Schema map
----------
``games``                 one row per Roblox universe (unique on ``universe_id``)
``game_snapshots``        point-in-time metric history powering growth detection
``game_scores``           every computed Scout Score with its explanation
``partner_matches``       per-game partner ranking with reasons and exclusions
``lead_partners``         which leads went to which partner, and the deal terms
``lead_notes``            free-form scout notes
``lead_status_history``   append-only CRM audit trail
``alerts``                every Discord notification attempt (incl. cooldowns)
``scout_users``           Discord humans known to the bot
``settings``              runtime-editable configuration overrides
``discovery_runs``        per-scan telemetry used by ``/stats`` and the dashboard

Notes on honesty of data
------------------------
Columns whose upstream metric may be unavailable are nullable and are simply
left as ``NULL``. ScoutForge never invents historical values; ``game_snapshots``
only contains rows for scans that actually observed the game.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from scoutforge.enums import LeadStatus, PartnerOutreach, RunStatus
from scoutforge.utils import utcnow

#: Native ``JSON`` on MySQL 8 (``LONGTEXT`` + ``json`` validity check) and plain
#: JSON everywhere else, so tests can still run against sqlite.
JSONType = JSON().with_variant(mysql.JSON(), "mysql")


class Base(DeclarativeBase):
    """Declarative base with a portable JSON type."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
    }


def _pk() -> Mapped[int]:
    return mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )


def _ts(**kwargs: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class Game(Base):
    """A Roblox universe (a "game") tracked by ScoutForge."""

    __tablename__ = "games"

    id: Mapped[int] = _pk()

    # --- identity -------------------------------------------------------
    universe_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    place_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    game_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    roblox_url: Mapped[str | None] = mapped_column(String(512))

    # --- creator --------------------------------------------------------
    creator_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    creator_name: Mapped[str | None] = mapped_column(String(256))
    creator_type: Mapped[str | None] = mapped_column(String(32))
    group_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    group_name: Mapped[str | None] = mapped_column(String(256))
    creator_url: Mapped[str | None] = mapped_column(String(512))

    # --- live metrics (last observation; never back-filled) --------------
    current_ccu: Mapped[int | None] = mapped_column(Integer, index=True)
    lifetime_visits: Mapped[int | None] = mapped_column(BigInteger, index=True)
    favorites: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    dislikes: Mapped[int | None] = mapped_column(Integer)
    voice_chat_visits: Mapped[int | None] = mapped_column(Integer)
    playing_now: Mapped[int | None] = mapped_column(Integer)

    # --- media / socials ------------------------------------------------
    thumbnail_url: Mapped[str | None] = mapped_column(String(512))
    public_discord_url: Mapped[str | None] = mapped_column(String(512))

    # --- upstream timestamps -------------------------------------------
    game_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    game_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- ScoutForge bookkeeping ----------------------------------------
    first_seen_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = _ts(
        nullable=False, default=utcnow, index=True, onupdate=utcnow
    )
    peak_ccu: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovery_source: Mapped[str | None] = mapped_column(String(64))
    missing_metrics: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    # --- CRM ------------------------------------------------------------
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=LeadStatus.NEW, index=True
    )
    is_lead: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    lead_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_scout_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    assigned_scout_name: Mapped[str | None] = mapped_column(String(128))
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_to_worldent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deal_status: Mapped[str | None] = mapped_column(String(64))
    deal_value: Mapped[float | None] = mapped_column(Float)
    rejected_reason: Mapped[str | None] = mapped_column(Text)

    # --- current scoring (denormalised for fast dashboards) --------------
    current_score: Mapped[float | None] = mapped_column(Float, index=True)
    current_level: Mapped[str | None] = mapped_column(String(16), index=True)
    score_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- relations ------------------------------------------------------
    snapshots: Mapped[list[GameSnapshot]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    scores: Mapped[list[GameScore]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    notes: Mapped[list[LeadNote]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    status_history: Mapped[list[LeadStatusHistory]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    partner_matches: Mapped[list[PartnerMatch]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )
    lead_partners: Mapped[list[LeadPartner]] = relationship(
        back_populates="game", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_games_lead_status_created", "is_lead", "status", "first_seen_at"),
        Index("ix_games_score_level", "current_score", "current_level"),
        Index("ix_games_ccu_visits", "current_ccu", "lifetime_visits"),
        {"comment": "Discovered Roblox games tracked by ScoutForge"},
    )

    @property
    def ccu_to_visits_ratio(self) -> float | None:
        if not self.current_ccu or not self.lifetime_visits:
            return None
        return self.current_ccu / self.lifetime_visits

    @property
    def public_url(self) -> str:
        return self.roblox_url or f"https://www.roblox.com/games/{self.universe_id}"


class GameSnapshot(Base):
    """Immutable observation of a game's metrics at a point in time."""

    __tablename__ = "game_snapshots"

    id: Mapped[int] = _pk()
    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    current_ccu: Mapped[int | None] = mapped_column(Integer)
    lifetime_visits: Mapped[int | None] = mapped_column(BigInteger)
    favorites: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    dislikes: Mapped[int | None] = mapped_column(Integer)
    playing_now: Mapped[int | None] = mapped_column(Integer)
    voice_chat_visits: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(64))

    game: Mapped[Game] = relationship(back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("universe_id", "captured_at", name="uq_snapshot_universe_time"),
        Index("ix_snapshots_universe_time", "universe_id", "captured_at"),
    )


class GameScore(Base):
    """A computed Scout Score plus the full explanation of how it was derived."""

    __tablename__ = "game_scores"

    id: Mapped[int] = _pk()
    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    computed_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    components: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    caveats: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    ccu_to_visits_ratio: Mapped[float | None] = mapped_column(Float)
    growth_1h_pct: Mapped[float | None] = mapped_column(Float)
    growth_6h_pct: Mapped[float | None] = mapped_column(Float)
    growth_24h_pct: Mapped[float | None] = mapped_column(Float)
    ccus_per_day: Mapped[float | None] = mapped_column(Float)

    game: Mapped[Game] = relationship(back_populates="scores")

    __table_args__ = (Index("ix_scores_universe_time", "universe_id", "computed_at"),)


class PartnerMatch(Base):
    """Latest partner verdict for one game, with the full explanation.

    One row per ``(universe_id, partner_key)``; re-running the matcher overwrites
    it in place so the table always answers "who fits this game right now" in a
    single indexed read. ``reasons`` and ``exclusions`` are stored verbatim so
    the dashboard and Discord embeds can quote the matcher instead of
    re-deriving anything.
    """

    __tablename__ = "partner_matches"

    id: Mapped[int] = _pk()

    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    partner_key: Mapped[str] = mapped_column(String(64), nullable=False)
    partner_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    revenue_share: Mapped[str | None] = mapped_column(String(128))
    deal_terms: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    exclusions: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    matched_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    scout_score: Mapped[float | None] = mapped_column(Float)
    game_stage: Mapped[str | None] = mapped_column(String(16))

    game: Mapped[Game] = relationship(back_populates="partner_matches")

    __table_args__ = (
        UniqueConstraint("universe_id", "partner_key", name="uq_partner_match_game_partner"),
        Index("ix_partner_matches_partner_score", "partner_key", "score"),
        Index("ix_partner_matches_eligible_score", "eligible", "score"),
    )


class LeadPartner(Base):
    """Which lead was routed to which partner, and what happened next.

    Manual contact proof is first-class: a scout records *how* the contact was
    made and when, because ScoutForge never messages anyone itself and a deal
    claim without a trail is worthless.
    """

    __tablename__ = "lead_partners"

    id: Mapped[int] = _pk()

    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    partner_key: Mapped[str] = mapped_column(String(64), nullable=False)
    partner_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    match_score: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PartnerOutreach.NEW, index=True
    )
    revenue_share_pct: Mapped[float | None] = mapped_column(Float)
    deal_terms: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # --- manual contact proof -------------------------------------------
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    contact_method: Mapped[str | None] = mapped_column(String(64))
    contact_reference: Mapped[str | None] = mapped_column(String(256))
    contacted_by_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    contacted_by_name: Mapped[str | None] = mapped_column(String(128))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)

    game: Mapped[Game] = relationship(back_populates="lead_partners")

    __table_args__ = (
        UniqueConstraint("universe_id", "partner_key", name="uq_lead_partner_game_partner"),
        Index("ix_lead_partners_partner_status", "partner_key", "status"),
        Index("ix_lead_partners_universe_status", "universe_id", "status"),
    )


class LeadNote(Base):
    """A free-form note left by a scout."""

    __tablename__ = "lead_notes"

    id: Mapped[int] = _pk()
    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    author_name: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)

    game: Mapped[Game] = relationship(back_populates="notes")

    __table_args__ = (Index("ix_notes_universe_time", "universe_id", "created_at"),)


class LeadStatusHistory(Base):
    """Append-only CRM transition log."""

    __tablename__ = "lead_status_history"

    id: Mapped[int] = _pk()
    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    changed_by_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    changed_by_name: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)

    game: Mapped[Game] = relationship(back_populates="status_history")

    __table_args__ = (Index("ix_status_history_universe_time", "universe_id", "created_at"),)


class Alert(Base):
    """One alert evaluation - delivered or suppressed by the cooldown engine."""

    __tablename__ = "alerts"

    id: Mapped[int] = _pk()
    universe_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("games.universe_id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    previous_score: Mapped[float | None] = mapped_column(Float)
    score_delta: Mapped[float | None] = mapped_column(Float)
    ccu: Mapped[int | None] = mapped_column(Integer)
    ccu_spike_pct: Mapped[float | None] = mapped_column(Float)
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    suppressed_reason: Mapped[str | None] = mapped_column(String(64))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)

    game: Mapped[Game] = relationship(back_populates="alerts")

    __table_args__ = (Index("ix_alerts_universe_time", "universe_id", "created_at"),)


class ScoutUser(Base):
    """A Discord user known to ScoutForge."""

    __tablename__ = "scout_users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """Runtime-editable configuration override stored as JSON."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONType, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class DiscoveryRun(Base):
    """Telemetry for one scheduled discovery pass."""

    __tablename__ = "discovery_runs"

    id: Mapped[int] = _pk()
    started_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RunStatus.RUNNING, index=True
    )
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False, default="scheduler")
    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    games_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scores_computed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerts_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerts_suppressed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    api_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    rejection_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    source_stats: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    @property
    def total_candidates(self) -> int:
        return self.games_accepted + self.games_rejected


class ApiCallStat(Base):
    """Rolling per-source API call counters surfaced by ``/stats``."""

    __tablename__ = "api_call_stats"

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    captured_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, index=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    __table_args__ = (Index("ix_api_stats_source_time", "source", "captured_at"),)


class ServerStat(Base):
    """Singleton-ish key/value counters for /stats and dashboard KPIs."""

    __tablename__ = "server_stats"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    updated_at: Mapped[datetime] = _ts(nullable=False, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


__all__ = [
    "Alert",
    "ApiCallStat",
    "Base",
    "DiscoveryRun",
    "Game",
    "GameScore",
    "GameSnapshot",
    "LeadNote",
    "LeadPartner",
    "LeadStatusHistory",
    "PartnerMatch",
    "ScoutUser",
    "ServerStat",
    "Setting",
]
