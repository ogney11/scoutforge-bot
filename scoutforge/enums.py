"""Core domain enumerations shared across ScoutForge subsystems."""

from __future__ import annotations

from enum import StrEnum


class LeadStatus(StrEnum):
    """CRM lifecycle status for a scouting lead.

    Transitions are validated by :class:`scoutforge.services.crm_service.LeadStatusMachine`
    so arbitrary or accidental statuses cannot be entered from Discord.
    """

    NEW = "NEW"
    RESEARCHED = "RESEARCHED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    SENT_TO_WORLDENT = "SENT_TO_WORLDENT"
    NEGOTIATING = "NEGOTIATING"
    DEAL = "DEAL"
    REJECTED = "REJECTED"
    NOT_INTERESTED = "NOT_INTERESTED"


class AlertLevel(StrEnum):
    """Severity of a scouting alert."""

    NORMAL = "NORMAL"
    WARM = "WARM"
    HOT = "HOT"
    EXTREME = "EXTREME"

    @property
    def rank(self) -> int:
        return _ALERT_RANK[self]

    @property
    def notifies_discord(self) -> bool:
        """HOT and EXTREME always notify; WARM is notify-configurable."""
        return self in (AlertLevel.HOT, AlertLevel.EXTREME)


_ALERT_RANK: dict[AlertLevel, int] = {
    AlertLevel.NORMAL: 0,
    AlertLevel.WARM: 1,
    AlertLevel.HOT: 2,
    AlertLevel.EXTREME: 3,
}


class RunStatus(StrEnum):
    """Outcome of a discovery run."""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AlertState(StrEnum):
    """Master switch for alert delivery, toggled with ``/alerts toggle``.

    Modelled as an enum rather than a bare string so Discord renders a real
    picker: a plain ``str`` parameter would accept anything.
    """

    ON = "on"
    OFF = "off"


class PartnerOutreach(StrEnum):
    """Where a lead sits with one particular partner.

    Deliberately separate from :class:`LeadStatus`: the CRM status tracks the
    game, this tracks the relationship with a single partner, and one game can
    be in several of these at once.
    """

    NEW = "NEW"
    SHORTLISTED = "SHORTLISTED"
    CONTACTED = "CONTACTED"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    NEGOTIATING = "NEGOTIATING"
    DEAL = "DEAL"
    PASSED = "PASSED"
    DECLINED = "DECLINED"


__all__ = [
    "AlertLevel",
    "AlertState",
    "LeadStatus",
    "PartnerOutreach",
    "RunStatus",
]
