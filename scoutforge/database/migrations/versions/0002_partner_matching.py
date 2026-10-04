"""partner matching tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03 19:40:00.000000

Adds the two tables the configurable partner matcher needs:

``partner_matches``  latest verdict per (game, partner), with reasons and the
                     exclusions that knocked a partner out
``lead_partners``    the manual outreach trail: who a human routed a lead to,
                     what the deal terms were, and the evidence of contact

Both use the shared :data:`scoutforge.database.models.JSONType` so the DDL is
native ``JSON`` on MySQL and plain JSON on sqlite.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from scoutforge.database.models import JSONType

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partner_matches",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("universe_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_key", sa.String(length=64), nullable=False),
        sa.Column("partner_name", sa.String(length=128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("revenue_share", sa.String(length=128), nullable=True),
        sa.Column("deal_terms", JSONType, nullable=False),
        sa.Column("reasons", JSONType, nullable=False),
        sa.Column("exclusions", JSONType, nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scout_score", sa.Float(), nullable=True),
        sa.Column("game_stage", sa.String(length=16), nullable=True),
        sa.ForeignKeyConstraint(["universe_id"], ["games.universe_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("universe_id", "partner_key", name="uq_partner_match_game_partner"),
        comment="Latest partner verdict per game, with reasons and exclusions",
    )
    with op.batch_alter_table("partner_matches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_partner_matches_eligible"), ["eligible"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_partner_matches_matched_at"), ["matched_at"], unique=False
        )
        batch_op.create_index(
            "ix_partner_matches_eligible_score", ["eligible", "score"], unique=False
        )
        batch_op.create_index(
            "ix_partner_matches_partner_score", ["partner_key", "score"], unique=False
        )

    op.create_table(
        "lead_partners",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("universe_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_key", sa.String(length=64), nullable=False),
        sa.Column("partner_name", sa.String(length=128), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revenue_share_pct", sa.Float(), nullable=True),
        sa.Column("deal_terms", JSONType, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("contacted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_method", sa.String(length=64), nullable=True),
        sa.Column("contact_reference", sa.String(length=256), nullable=True),
        sa.Column("contacted_by_id", sa.BigInteger(), nullable=True),
        sa.Column("contacted_by_name", sa.String(length=128), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["universe_id"], ["games.universe_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("universe_id", "partner_key", name="uq_lead_partner_game_partner"),
        comment="Manual partner outreach: routing, deal terms and contact evidence",
    )
    with op.batch_alter_table("lead_partners", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_lead_partners_contacted_by_id"), ["contacted_by_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_lead_partners_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_lead_partners_status"), ["status"], unique=False)
        batch_op.create_index(
            "ix_lead_partners_partner_status", ["partner_key", "status"], unique=False
        )
        batch_op.create_index(
            "ix_lead_partners_universe_status", ["universe_id", "status"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("lead_partners", schema=None) as batch_op:
        batch_op.drop_index("ix_lead_partners_universe_status")
        batch_op.drop_index("ix_lead_partners_partner_status")
        batch_op.drop_index(batch_op.f("ix_lead_partners_status"))
        batch_op.drop_index(batch_op.f("ix_lead_partners_created_at"))
        batch_op.drop_index(batch_op.f("ix_lead_partners_contacted_by_id"))

    op.drop_table("lead_partners")
    with op.batch_alter_table("partner_matches", schema=None) as batch_op:
        batch_op.drop_index("ix_partner_matches_partner_score")
        batch_op.drop_index("ix_partner_matches_eligible_score")
        batch_op.drop_index(batch_op.f("ix_partner_matches_matched_at"))
        batch_op.drop_index(batch_op.f("ix_partner_matches_eligible"))

    op.drop_table("partner_matches")
