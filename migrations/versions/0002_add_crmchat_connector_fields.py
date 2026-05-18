"""add crmchat connector fields

Revision ID: 0002_add_crmchat_connector_fields
Revises: 0001_create_base_tables
Create Date: 2026-05-18 00:00:01.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_crmchat_connector_fields"
down_revision: Union[str, None] = "0001_create_base_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("crmchat_organization_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("crmchat_workspace_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f("ix_accounts_crmchat_organization_id"),
        "accounts",
        ["crmchat_organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_accounts_crmchat_workspace_id"),
        "accounts",
        ["crmchat_workspace_id"],
        unique=False,
    )

    op.add_column(
        "dialogs", sa.Column("telegram_peer_type", sa.String(length=50), nullable=True)
    )
    op.add_column(
        "dialogs", sa.Column("telegram_peer_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "dialogs",
        sa.Column("telegram_access_hash", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "dialogs", sa.Column("telegram_username", sa.String(length=255), nullable=True)
    )
    op.create_index(
        op.f("ix_dialogs_telegram_peer_type"),
        "dialogs",
        ["telegram_peer_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dialogs_telegram_peer_id"),
        "dialogs",
        ["telegram_peer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dialogs_telegram_username"),
        "dialogs",
        ["telegram_username"],
        unique=False,
    )

    op.add_column(
        "outbound_send_logs",
        sa.Column("telegram_random_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        op.f("ix_outbound_send_logs_telegram_random_id"),
        "outbound_send_logs",
        ["telegram_random_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_outbound_send_logs_telegram_random_id"),
        table_name="outbound_send_logs",
    )
    op.drop_column("outbound_send_logs", "telegram_random_id")

    op.drop_index(op.f("ix_dialogs_telegram_username"), table_name="dialogs")
    op.drop_index(op.f("ix_dialogs_telegram_peer_id"), table_name="dialogs")
    op.drop_index(op.f("ix_dialogs_telegram_peer_type"), table_name="dialogs")
    op.drop_column("dialogs", "telegram_username")
    op.drop_column("dialogs", "telegram_access_hash")
    op.drop_column("dialogs", "telegram_peer_id")
    op.drop_column("dialogs", "telegram_peer_type")

    op.drop_index(op.f("ix_accounts_crmchat_workspace_id"), table_name="accounts")
    op.drop_index(op.f("ix_accounts_crmchat_organization_id"), table_name="accounts")
    op.drop_column("accounts", "crmchat_workspace_id")
    op.drop_column("accounts", "crmchat_organization_id")
