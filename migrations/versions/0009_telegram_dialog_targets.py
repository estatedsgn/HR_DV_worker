"""create telegram dialog targets table

Revision ID: 0009_telegram_dialog_targets
Revises: 0008_inbound_event_leases
Create Date: 2026-05-20 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_telegram_dialog_targets"
down_revision: Union[str, None] = "0008_inbound_event_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_dialog_targets",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_peer_type", sa.String(length=50), nullable=False),
        sa.Column("telegram_peer_id", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_telegram_dialog_targets_account_id"), "telegram_dialog_targets", ["account_id"], unique=False)
    op.create_index(op.f("ix_telegram_dialog_targets_telegram_peer_type"), "telegram_dialog_targets", ["telegram_peer_type"], unique=False)
    op.create_index(op.f("ix_telegram_dialog_targets_telegram_peer_id"), "telegram_dialog_targets", ["telegram_peer_id"], unique=False)
    op.create_index(op.f("ix_telegram_dialog_targets_is_active"), "telegram_dialog_targets", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_dialog_targets_is_active"), table_name="telegram_dialog_targets")
    op.drop_index(op.f("ix_telegram_dialog_targets_telegram_peer_id"), table_name="telegram_dialog_targets")
    op.drop_index(op.f("ix_telegram_dialog_targets_telegram_peer_type"), table_name="telegram_dialog_targets")
    op.drop_index(op.f("ix_telegram_dialog_targets_account_id"), table_name="telegram_dialog_targets")
    op.drop_table("telegram_dialog_targets")
