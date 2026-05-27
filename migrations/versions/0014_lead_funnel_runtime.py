"""add langgraph funnel runtime

Revision ID: 0014_lead_funnel_runtime
Revises: 0013_outbound_voice_jobs
Create Date: 2026-05-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0014_lead_funnel_runtime"
down_revision: Union[str, None] = "0013_outbound_voice_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "lead_funnel_runtime",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.String(length=120), nullable=False),
        sa.Column("stage", sa.String(length=80), server_default="bootstrap", nullable=False),
        sa.Column("status", sa.String(length=50), server_default="active", nullable=False),
        sa.Column("last_processed_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_candidate_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_candidate_typing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("debounce_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_goal", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_processed_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", name="uq_lead_funnel_runtime_lead_id"),
        sa.UniqueConstraint("thread_id", name="uq_lead_funnel_runtime_thread_id"),
    )
    for column in [
        "lead_id",
        "dialog_id",
        "thread_id",
        "stage",
        "status",
        "last_processed_message_id",
        "last_candidate_activity_at",
        "last_candidate_typing_at",
        "debounce_until",
    ]:
        op.create_index(op.f(f"ix_lead_funnel_runtime_{column}"), "lead_funnel_runtime", [column], unique=False)


def downgrade() -> None:
    for column in [
        "debounce_until",
        "last_candidate_typing_at",
        "last_candidate_activity_at",
        "last_processed_message_id",
        "status",
        "stage",
        "thread_id",
        "dialog_id",
        "lead_id",
    ]:
        op.drop_index(op.f(f"ix_lead_funnel_runtime_{column}"), table_name="lead_funnel_runtime")
    op.drop_table("lead_funnel_runtime")
