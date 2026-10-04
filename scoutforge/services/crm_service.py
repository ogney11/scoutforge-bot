"""CRM service: lead lifecycle rules, status transitions and lead queries.

Status values are a closed enum - arbitrary strings can never enter the
database, from Discord or from any other code path. Transitions are validated
against an explicit state machine so a lead cannot silently jump from ``NEW`` to
``DEAL`` without the intermediate steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scoutforge.config.logging import get_logger, log_event
from scoutforge.database.models import Game, LeadNote, LeadStatusHistory
from scoutforge.database.repositories import (
    GameRepository,
    LeadNoteRepository,
    LeadStatusHistoryRepository,
)
from scoutforge.enums import AlertLevel, LeadStatus
from scoutforge.utils import utcnow

log = get_logger(__name__)


class InvalidTransitionError(ValueError):
    """Raised when a status change is not allowed."""

    def __init__(self, current: LeadStatus, target: LeadStatus, allowed: set[LeadStatus]) -> None:
        allowed_text = ", ".join(sorted(s.value for s in allowed)) or "none"
        super().__init__(
            f"Cannot move lead from {current.value} to {target.value}. "
            f"Allowed from {current.value}: {allowed_text}"
        )
        self.current = current
        self.target = target
        self.allowed = allowed


class UnknownStatusError(ValueError):
    """Raised when a status string is not part of the enum."""


#: Explicit forward-flow of the CRM pipeline.
ALLOWED_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    LeadStatus.NEW: {
        LeadStatus.RESEARCHED,
        LeadStatus.CONTACTED,
        LeadStatus.REJECTED,
        LeadStatus.NOT_INTERESTED,
    },
    LeadStatus.RESEARCHED: {
        LeadStatus.CONTACTED,
        LeadStatus.REJECTED,
        LeadStatus.NOT_INTERESTED,
    },
    LeadStatus.CONTACTED: {
        LeadStatus.REPLIED,
        LeadStatus.INTERESTED,
        LeadStatus.NEGOTIATING,
        LeadStatus.NOT_INTERESTED,
        LeadStatus.REJECTED,
    },
    LeadStatus.REPLIED: {
        LeadStatus.INTERESTED,
        LeadStatus.NEGOTIATING,
        LeadStatus.NOT_INTERESTED,
        LeadStatus.REJECTED,
    },
    LeadStatus.INTERESTED: {
        LeadStatus.SENT_TO_WORLDENT,
        LeadStatus.NEGOTIATING,
        LeadStatus.NOT_INTERESTED,
    },
    LeadStatus.SENT_TO_WORLDENT: {
        LeadStatus.NEGOTIATING,
        LeadStatus.DEAL,
        LeadStatus.NOT_INTERESTED,
    },
    LeadStatus.NEGOTIATING: {
        LeadStatus.DEAL,
        LeadStatus.NOT_INTERESTED,
    },
    LeadStatus.DEAL: {LeadStatus.NOT_INTERESTED},
    LeadStatus.REJECTED: {LeadStatus.NEW, LeadStatus.NOT_INTERESTED},
    LeadStatus.NOT_INTERESTED: {LeadStatus.NEW, LeadStatus.INTERESTED},
}

#: Statuses that mean the scout does not want to see this lead by default.
CLOSED_STATUSES = {LeadStatus.REJECTED, LeadStatus.NOT_INTERESTED}

#: Statuses where the developer has actually been contacted (human-in-the-loop).
CONTACTED_STATUSES = {
    LeadStatus.CONTACTED,
    LeadStatus.REPLIED,
    LeadStatus.INTERESTED,
    LeadStatus.SENT_TO_WORLDENT,
    LeadStatus.NEGOTIATING,
    LeadStatus.DEAL,
}


def parse_status(value: str) -> LeadStatus:
    """Parse a user supplied status string into the enum.

    Raises:
        UnknownStatusError: when the string is not a valid status.
    """
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "SENT_TO_WORLD_ENT": LeadStatus.SENT_TO_WORLDENT,
        "WORLDENT": LeadStatus.SENT_TO_WORLDENT,
        "WORLD_ENT": LeadStatus.SENT_TO_WORLDENT,
        "SENTTOWORLDENT": LeadStatus.SENT_TO_WORLDENT,
        "NOT_INTERESTED_IN": LeadStatus.NOT_INTERESTED,
        "NO": LeadStatus.NOT_INTERESTED,
        "PASS": LeadStatus.NOT_INTERESTED,
        "PASSED": LeadStatus.NOT_INTERESTED,
        "INTERESTED_IN": LeadStatus.INTERESTED,
        "CLOSED": LeadStatus.REJECTED,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return LeadStatus(normalized)
    except ValueError as exc:
        raise UnknownStatusError(
            f"'{value}' is not a valid status. Valid statuses: "
            f"{', '.join(s.value for s in LeadStatus)}"
        ) from exc


def allowed_transitions(current: LeadStatus) -> set[LeadStatus]:
    """Statuses reachable from ``current``."""
    return set(ALLOWED_TRANSITIONS.get(current, set()))


def can_transition(current: LeadStatus, target: LeadStatus) -> bool:
    """Whether ``current -> target`` is allowed (identical status is a no-op)."""
    if current is target:
        return True
    return target in allowed_transitions(current)


@dataclass(slots=True)
class LeadView:
    """Everything the Discord embeds and dashboard need about one lead."""

    game: Game
    score: float | None
    level: AlertLevel | None
    growth: dict[str, Any] | None
    notes: list[LeadNote]
    history: list[LeadStatusHistory]
    alert_count: int = 0

    @property
    def universe_id(self) -> int:
        return self.game.universe_id

    @property
    def status(self) -> LeadStatus:
        return LeadStatus(self.game.status)

    @property
    def display_name(self) -> str:
        return self.game.game_name or f"Universe {self.game.universe_id}"

    @property
    def is_contacted(self) -> bool:
        return self.status in CONTACTED_STATUSES


class LeadService:
    """Read and write operations for scouting leads."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.games = GameRepository(session)
        self.notes = LeadNoteRepository(session)
        self.history = LeadStatusHistoryRepository(session)

    async def get_lead(self, universe_id: int, *, require_lead: bool = True) -> Game | None:
        game = await self.games.get(universe_id)
        if game is None:
            return None
        if require_lead and not game.is_lead:
            return None
        return game

    async def change_status(
        self,
        universe_id: int,
        target: str | LeadStatus,
        *,
        actor_id: int | None = None,
        actor_name: str | None = None,
        reason: str | None = None,
        force: bool = False,
    ) -> tuple[Game, LeadStatusHistory]:
        """Move a lead to ``target``, validating the transition.

        Args:
            universe_id: Target universe.
            target: New status, as an enum or user string.
            actor_id: Discord id performing the change.
            actor_name: Discord name performing the change.
            reason: Optional free-text justification stored in the audit trail.
            force: Skip transition validation (admin override).
        """
        new_status = target if isinstance(target, LeadStatus) else parse_status(target)
        game = await self.games.get(universe_id)
        if game is None:
            raise LookupError(f"Unknown universe_id {universe_id}")

        current = LeadStatus(game.status) if game.status else LeadStatus.NEW
        if not force and not can_transition(current, new_status):
            raise InvalidTransitionError(current, new_status, allowed_transitions(current))

        entry = await self.games.update_status(
            game, new_status, actor_id=actor_id, actor_name=actor_name, reason=reason
        )
        log_event(
            log,
            20,
            "STATUS_CHANGE",
            universe=universe_id,
            **{"from": current.value, "to": new_status.value, "by": actor_name or actor_id},
        )
        return game, entry

    async def add_note(
        self,
        universe_id: int,
        body: str,
        *,
        author_id: int | None = None,
        author_name: str | None = None,
    ) -> LeadNote:
        game = await self.games.get(universe_id)
        if game is None:
            raise LookupError(f"Unknown universe_id {universe_id}")
        text = body.strip()
        if not text:
            raise ValueError("Note body cannot be empty")
        return await self.notes.add(
            universe_id, text[:4000], author_id=author_id, author_name=author_name
        )

    async def mark_interest(self, universe_id: int, *, interested: bool = True) -> None:
        """Shortcut used by dashboard buttons."""
        await self.change_status(
            universe_id,
            LeadStatus.INTERESTED if interested else LeadStatus.NOT_INTERESTED,
            reason="dashboard action",
            force=True,
        )

    async def reopen(self, universe_id: int, *, actor_name: str | None = None) -> Game:
        """Move a closed lead back into the pipeline."""
        game, _entry = await self.change_status(
            universe_id, LeadStatus.NEW, actor_name=actor_name, reason="reopened", force=True
        )
        return game

    async def build_view(
        self,
        game: Game,
        *,
        growth: dict[str, Any] | None = None,
        notes_limit: int = 10,
        history_limit: int = 20,
    ) -> LeadView:
        notes = await self.notes.list_for(game.universe_id, limit=notes_limit)
        history = await self.history.list_for(game.universe_id, limit=history_limit)
        return LeadView(
            game=game,
            score=game.current_score,
            level=AlertLevel(game.current_level) if game.current_level else None,
            growth=growth,
            notes=notes,
            history=history,
        )

    async def timeline(self, universe_id: int, *, limit: int = 25) -> list[dict[str, Any]]:
        """Merged notes + status history, newest first."""
        notes = await self.notes.list_for(universe_id, limit=limit)
        history = await self.history.list_for(universe_id, limit=limit)
        entries: list[dict[str, Any]] = [
            {
                "kind": "note",
                "at": note.created_at,
                "author": note.author_name,
                "text": note.body,
            }
            for note in notes
        ]
        entries.extend(
            {
                "kind": "status",
                "at": item.created_at,
                "author": item.changed_by_name,
                "text": (
                    f"{item.from_status or 'new'} → {item.to_status}"
                    + (f" ({item.reason})" if item.reason else "")
                ),
            }
            for item in history
        )
        entries.sort(key=lambda e: e["at"], reverse=True)
        return entries[:limit]

    async def bulk_status_counts(self) -> dict[str, int]:
        return await self.games.status_counts()

    async def contact_candidates(self, limit: int = 25) -> list[Game]:
        """Highest scoring leads that the scout has not contacted yet."""
        return await self.games.list_leads(
            statuses=[LeadStatus.NEW, LeadStatus.RESEARCHED], order_by="score", limit=limit
        )

    async def stale_leads(self, *, older_than_days: int, limit: int = 25) -> list[Game]:
        """Leads that have not been contacted despite being hot for a while."""
        cutoff = utcnow() - timedelta(days=older_than_days)
        candidates = await self.games.list_leads(order_by="score", limit=limit * 4)
        return [
            game
            for game in candidates
            if game.lead_created_at
            and (game.lead_created_at <= cutoff)
            and LeadStatus(game.status) not in CONTACTED_STATUSES
            and LeadStatus(game.status) not in CLOSED_STATUSES
        ][:limit]


def status_choices() -> list[tuple[str, str]]:
    """``(label, value)`` pairs for the Discord autocomplete/choice API."""
    labels = {
        LeadStatus.NEW: "New - just discovered",
        LeadStatus.RESEARCHED: "Researched - looked into, not contacted",
        LeadStatus.CONTACTED: "Contacted - scout reached out",
        LeadStatus.REPLIED: "Replied - developer responded",
        LeadStatus.INTERESTED: "Interested - developer wants to talk",
        LeadStatus.SENT_TO_WORLDENT: "Sent to WorldEnt",
        LeadStatus.NEGOTIATING: "Negotiating",
        LeadStatus.DEAL: "Deal - closed won",
        LeadStatus.REJECTED: "Rejected - not a fit",
        LeadStatus.NOT_INTERESTED: "Not interested",
    }
    return [(labels[status], status.value) for status in LeadStatus]


def next_status_options(current: LeadStatus) -> list[tuple[str, str]]:
    """Choices valid from ``current``."""
    allowed = allowed_transitions(current)
    if not allowed:
        return []
    return [(status.value, status.value) for status in sorted(allowed, key=lambda s: s.value)]


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLOSED_STATUSES",
    "CONTACTED_STATUSES",
    "InvalidTransitionError",
    "LeadService",
    "LeadView",
    "UnknownStatusError",
    "allowed_transitions",
    "can_transition",
    "next_status_options",
    "parse_status",
    "status_choices",
]
