"""add brain v2 turn debounce fields

Revision ID: 0012_brain_v2_turn_debounce
Revises: 0011_brain_v2_layer
Create Date: 2026-05-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0012_brain_v2_turn_debounce"
down_revision: Union[str, None] = "0011_brain_v2_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("lead_brain_state", sa.Column("last_candidate_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lead_brain_state", sa.Column("last_candidate_typing_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lead_brain_state", sa.Column("debounce_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("lead_brain_state", sa.Column("last_processed_message_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_lead_brain_state_last_processed_message_id_messages",
        "lead_brain_state",
        "messages",
        ["last_processed_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_lead_brain_state_debounce_until"), "lead_brain_state", ["debounce_until"], unique=False)
    op.create_index(
        op.f("ix_lead_brain_state_last_processed_message_id"),
        "lead_brain_state",
        ["last_processed_message_id"],
        unique=False,
    )
    op.add_column(
        "brain_runs",
        sa.Column("message_batch", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("brain_runs", "message_batch")
    op.drop_index(op.f("ix_lead_brain_state_last_processed_message_id"), table_name="lead_brain_state")
    op.drop_index(op.f("ix_lead_brain_state_debounce_until"), table_name="lead_brain_state")
    op.drop_constraint("fk_lead_brain_state_last_processed_message_id_messages", "lead_brain_state", type_="foreignkey")
    op.drop_column("lead_brain_state", "last_processed_message_id")
    op.drop_column("lead_brain_state", "debounce_until")
    op.drop_column("lead_brain_state", "last_candidate_typing_at")
    op.drop_column("lead_brain_state", "last_candidate_activity_at")
