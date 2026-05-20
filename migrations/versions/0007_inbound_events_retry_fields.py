"""add retry scheduling fields to inbound events

Revision ID: 0007_inbound_events_retry_fields
Revises: 0006_inbound_events_foreign_keys
Create Date: 2026-05-20 01:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_inbound_events_retry_fields"
down_revision: Union[str, None] = "0006_inbound_events_foreign_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inbound_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inbound_events",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
    )
    op.add_column(
        "inbound_events",
        sa.Column("last_error_type", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_inbound_events_next_attempt_at"),
        "inbound_events",
        ["next_attempt_at"],
        unique=False,
    )
    op.alter_column("inbound_events", "max_attempts", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_inbound_events_next_attempt_at"), table_name="inbound_events")
    op.drop_column("inbound_events", "last_error_type")
    op.drop_column("inbound_events", "max_attempts")
    op.drop_column("inbound_events", "next_attempt_at")
