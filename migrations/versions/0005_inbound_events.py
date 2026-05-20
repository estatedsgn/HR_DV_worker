"""create inbound events table

Revision ID: 0005_inbound_events
Revises: 0004_polling_runs
Create Date: 2026-05-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0005_inbound_events"
down_revision: Union[str, None] = "0004_polling_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_events",
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=True),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_inbound_events_source"), "inbound_events", ["source"], unique=False)
    op.create_index(op.f("ix_inbound_events_status"), "inbound_events", ["status"], unique=False)
    op.create_index(op.f("ix_inbound_events_external_event_id"), "inbound_events", ["external_event_id"], unique=True)
    op.create_index(op.f("ix_inbound_events_external_message_id"), "inbound_events", ["external_message_id"], unique=True)
    op.create_index(op.f("ix_inbound_events_account_id"), "inbound_events", ["account_id"], unique=False)
    op.create_index(op.f("ix_inbound_events_dialog_id"), "inbound_events", ["dialog_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_inbound_events_dialog_id"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_account_id"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_external_message_id"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_external_event_id"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_status"), table_name="inbound_events")
    op.drop_index(op.f("ix_inbound_events_source"), table_name="inbound_events")
    op.drop_table("inbound_events")
