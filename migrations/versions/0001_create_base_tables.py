"""create base tables

Revision ID: 0001_create_base_tables
Revises: 
Create Date: 2026-05-18 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001_create_base_tables"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("crmchat_account_id", sa.String(length=255), nullable=False),
        sa.Column("telegram_username", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_accounts_crmchat_account_id"), "accounts", ["crmchat_account_id"], unique=True)

    op.create_table(
        "dialogs",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("crmchat_dialog_id", sa.String(length=255), nullable=False),
        sa.Column("lead_external_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("memory_summary", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dialogs_account_id"), "dialogs", ["account_id"], unique=False)
    op.create_index(op.f("ix_dialogs_crmchat_dialog_id"), "dialogs", ["crmchat_dialog_id"], unique=True)
    op.create_index(op.f("ix_dialogs_lead_external_id"), "dialogs", ["lead_external_id"], unique=False)

    op.create_table(
        "agent_action_logs",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_action_logs_dialog_id"), "agent_action_logs", ["dialog_id"], unique=False)

    op.create_table(
        "human_handoffs",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("assigned_to", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_human_handoffs_dialog_id"), "human_handoffs", ["dialog_id"], unique=False)

    op.create_table(
        "leads",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("qualification_status", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("next_step", sa.String(length=255), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_leads_dialog_id"), "leads", ["dialog_id"], unique=True)

    op.create_table(
        "messages",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("crmchat_message_id", sa.String(length=255), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("sender_type", sa.String(length=50), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_crmchat_message_id"), "messages", ["crmchat_message_id"], unique=True)
    op.create_index(op.f("ix_messages_dialog_id"), "messages", ["dialog_id"], unique=False)

    op.create_table(
        "outbound_send_logs",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("rate_limit_bucket", sa.String(length=255), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbound_send_logs_dialog_id"), "outbound_send_logs", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_outbound_send_logs_message_id"), "outbound_send_logs", ["message_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_outbound_send_logs_message_id"), table_name="outbound_send_logs")
    op.drop_index(op.f("ix_outbound_send_logs_dialog_id"), table_name="outbound_send_logs")
    op.drop_table("outbound_send_logs")
    op.drop_index(op.f("ix_messages_dialog_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_crmchat_message_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_leads_dialog_id"), table_name="leads")
    op.drop_table("leads")
    op.drop_index(op.f("ix_human_handoffs_dialog_id"), table_name="human_handoffs")
    op.drop_table("human_handoffs")
    op.drop_index(op.f("ix_agent_action_logs_dialog_id"), table_name="agent_action_logs")
    op.drop_table("agent_action_logs")
    op.drop_index(op.f("ix_dialogs_lead_external_id"), table_name="dialogs")
    op.drop_index(op.f("ix_dialogs_crmchat_dialog_id"), table_name="dialogs")
    op.drop_index(op.f("ix_dialogs_account_id"), table_name="dialogs")
    op.drop_table("dialogs")
    op.drop_index(op.f("ix_accounts_crmchat_account_id"), table_name="accounts")
    op.drop_table("accounts")
