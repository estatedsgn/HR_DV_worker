"""create telegram polling runs table

Revision ID: 0004_polling_runs
Revises: 0003_webhook_events
Create Date: 2026-05-18 00:00:03.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0004_polling_runs"
down_revision: Union[str, None] = "0003_webhook_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_polling_runs",
        sa.Column("crmchat_organization_id", sa.String(length=255), nullable=True),
        sa.Column("crmchat_workspace_id", sa.String(length=255), nullable=True),
        sa.Column("crmchat_account_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("dialogs_seen", sa.Integer(), nullable=False),
        sa.Column("dialogs_synced", sa.Integer(), nullable=False),
        sa.Column("messages_seen", sa.Integer(), nullable=False),
        sa.Column("messages_created", sa.Integer(), nullable=False),
        sa.Column("flood_wait_seconds", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        op.f("ix_telegram_polling_runs_crmchat_account_id"),
        "telegram_polling_runs",
        ["crmchat_account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_polling_runs_crmchat_organization_id"),
        "telegram_polling_runs",
        ["crmchat_organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_polling_runs_crmchat_workspace_id"),
        "telegram_polling_runs",
        ["crmchat_workspace_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_telegram_polling_runs_status"),
        "telegram_polling_runs",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_telegram_polling_runs_status"), table_name="telegram_polling_runs"
    )
    op.drop_index(
        op.f("ix_telegram_polling_runs_crmchat_workspace_id"),
        table_name="telegram_polling_runs",
    )
    op.drop_index(
        op.f("ix_telegram_polling_runs_crmchat_organization_id"),
        table_name="telegram_polling_runs",
    )
    op.drop_index(
        op.f("ix_telegram_polling_runs_crmchat_account_id"),
        table_name="telegram_polling_runs",
    )
    op.drop_table("telegram_polling_runs")
