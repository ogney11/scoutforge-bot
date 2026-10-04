"""CRM status state machine.

Outreach is human-in-the-loop, so the state machine is the guardrail: it must
permit only the documented pipeline order, never lose a lead, and always record
who moved it and why.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from scoutforge.enums import LeadStatus
from scoutforge.services.crm_service import (
    ALLOWED_TRANSITIONS,
    CONTACTED_STATUSES,
    InvalidTransitionError,
    UnknownStatusError,
    allowed_transitions,
    can_transition,
    parse_status,
)

CANONICAL_PATH = [
    LeadStatus.NEW,
    LeadStatus.RESEARCHED,
    LeadStatus.CONTACTED,
    LeadStatus.REPLIED,
    LeadStatus.INTERESTED,
    LeadStatus.SENT_TO_WORLDENT,
    LeadStatus.NEGOTIATING,
    LeadStatus.DEAL,
]


class TestParseStatus:
    @pytest.mark.parametrize("status", list(LeadStatus))
    def test_round_trips_every_status(self, status: LeadStatus) -> None:
        assert parse_status(status.value) is status

    def test_is_case_and_separator_insensitive(self) -> None:
        assert parse_status("sent to worldent") is LeadStatus.SENT_TO_WORLDENT
        assert parse_status("  contacted  ") is LeadStatus.CONTACTED
        assert parse_status("NOT-INTERESTED") is LeadStatus.NOT_INTERESTED

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("sent_to_world_ent", LeadStatus.SENT_TO_WORLDENT),
            ("worldent", LeadStatus.SENT_TO_WORLDENT),
            ("no", LeadStatus.NOT_INTERESTED),
            ("pass", LeadStatus.NOT_INTERESTED),
            ("closed", LeadStatus.REJECTED),
        ],
    )
    def test_aliases(self, raw: str, expected: LeadStatus) -> None:
        assert parse_status(raw) is expected

    def test_unknown_status_raises_with_help(self) -> None:
        with pytest.raises(UnknownStatusError) as excinfo:
            parse_status("banana")
        assert "banana" in str(excinfo.value)
        assert "valid statuses" in str(excinfo.value).lower()


class TestTransitions:
    def test_canonical_pipeline_is_fully_walkable(self) -> None:
        for current, nxt in pairwise(CANONICAL_PATH):
            assert can_transition(current, nxt), f"{current} -> {nxt} must be allowed"

    def test_same_status_is_a_no_op(self) -> None:
        for status in LeadStatus:
            assert can_transition(status, status) is True

    def test_cannot_skip_the_pipeline(self) -> None:
        assert can_transition(LeadStatus.NEW, LeadStatus.DEAL) is False
        assert can_transition(LeadStatus.NEW, LeadStatus.NEGOTIATING) is False
        assert can_transition(LeadStatus.CONTACTED, LeadStatus.SENT_TO_WORLDENT) is False

    def test_cannot_move_backwards_through_the_pipeline(self) -> None:
        assert can_transition(LeadStatus.INTERESTED, LeadStatus.CONTACTED) is False
        assert can_transition(LeadStatus.DEAL, LeadStatus.NEGOTIATING) is False

    def test_every_status_can_be_dropped(self) -> None:
        """A scout must always be able to record 'not interested' from anywhere."""
        for status in LeadStatus:
            assert can_transition(status, LeadStatus.NOT_INTERESTED), status

    def test_dead_ends_can_reopen(self) -> None:
        assert can_transition(LeadStatus.REJECTED, LeadStatus.NEW) is True
        assert can_transition(LeadStatus.NOT_INTERESTED, LeadStatus.INTERESTED) is True

    def test_transition_table_covers_every_status(self) -> None:
        assert set(ALLOWED_TRANSITIONS) == set(LeadStatus)

    def test_no_transition_targets_an_unknown_status(self) -> None:
        for targets in ALLOWED_TRANSITIONS.values():
            assert targets <= set(LeadStatus)


class TestContactedSet:
    def test_contacted_statuses_exclude_the_pre_outreach_ones(self) -> None:
        assert LeadStatus.NEW not in CONTACTED_STATUSES
        assert LeadStatus.RESEARCHED not in CONTACTED_STATUSES

    def test_contacted_statuses_include_the_outreach_ones(self) -> None:
        for status in (
            LeadStatus.CONTACTED,
            LeadStatus.REPLIED,
            LeadStatus.INTERESTED,
            LeadStatus.SENT_TO_WORLDENT,
            LeadStatus.NEGOTIATING,
            LeadStatus.DEAL,
        ):
            assert status in CONTACTED_STATUSES

    def test_contacted_set_is_not_closed_statuses(self) -> None:
        assert not CONTACTED_STATUSES & {LeadStatus.REJECTED, LeadStatus.NOT_INTERESTED}


class TestInvalidTransition:
    def test_error_carries_the_offending_pair(self) -> None:
        allowed = allowed_transitions(LeadStatus.NEW)
        error = InvalidTransitionError(LeadStatus.NEW, LeadStatus.DEAL, allowed)
        text = str(error)
        assert LeadStatus.NEW.value in text
        assert LeadStatus.DEAL.value in text
        # The message must tell the scout what they *can* do instead.
        assert LeadStatus.CONTACTED.value in text
        assert error.current is LeadStatus.NEW
        assert error.target is LeadStatus.DEAL
        assert error.allowed == allowed

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(InvalidTransitionError, ValueError)

    def test_unknown_status_error_is_a_value_error(self) -> None:
        assert issubclass(UnknownStatusError, ValueError)
