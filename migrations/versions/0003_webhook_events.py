"""create crmchat webhook events table

Revision ID: 0003_webhook_events
Revises: 0002_crmchat_fields
Create Date: 2026-05-18 00:00:02.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003_webhook_events"
down_revision: Union[str, None] = "0002_crmchat_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crmchat_webhook_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("workspace_id", sa.String(length=255), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_crmchat_webhook_events_event_id"),
        "crmchat_webhook_events",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_crmchat_webhook_events_event_type"),
        "crmchat_webhook_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_crmchat_webhook_events_status"),
        "crmchat_webhook_events",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_crmchat_webhook_events_workspace_id"),
        "crmchat_webhook_events",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_crmchat_webhook_events_workspace_id"),
        table_name="crmchat_webhook_events",
    )
    op.drop_index(
        op.f("ix_crmchat_webhook_events_status"),
        table_name="crmchat_webhook_events",
    )
    op.drop_index(
        op.f("ix_crmchat_webhook_events_event_type"),
        table_name="crmchat_webhook_events",
    )
    op.drop_index(
        op.f("ix_crmchat_webhook_events_event_id"),
        table_name="crmchat_webhook_events",
    )
    op.drop_table("crmchat_webhook_events")
