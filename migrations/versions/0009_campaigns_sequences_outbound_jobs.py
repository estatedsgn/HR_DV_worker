"""add campaigns, lead intake, sequence runs, outbound jobs

Revision ID: 0009_v1_sequences
Revises: 0008_inbound_event_leases
Create Date: 2026-05-21 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_v1_sequences"
down_revision: Union[str, None] = "0008_inbound_event_leases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("send_interval_seconds", sa.Integer(), server_default="300", nullable=False),
    )
    op.add_column(
        "accounts",
        sa.Column("send_jitter_seconds", sa.Integer(), server_default="60", nullable=False),
    )
    op.add_column("accounts", sa.Column("next_available_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("accounts", sa.Column("flood_wait_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("health_status", sa.String(length=50), server_default="healthy", nullable=False),
    )
    op.add_column("accounts", sa.Column("last_error_message", sa.Text(), nullable=True))

    op.create_table(
        "campaigns",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_campaigns_status"), "campaigns", ["status"], unique=False)
    op.create_index(op.f("ix_campaigns_is_default"), "campaigns", ["is_default"], unique=False)

    op.create_table(
        "campaign_steps",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(length=50), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=True),
        sa.Column("delay_seconds", sa.Integer(), nullable=False),
        sa.Column("wait_for_reply", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "position", name="uq_campaign_steps_campaign_position"),
    )
    op.create_index(op.f("ix_campaign_steps_campaign_id"), "campaign_steps", ["campaign_id"], unique=False)

    op.create_table(
        "lead_intake_events",
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_lead_id", sa.String(length=255), nullable=False),
        sa.Column("telegram_username", sa.String(length=255), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_lead_id", name="uq_lead_intake_source_external"),
    )
    op.create_index(op.f("ix_lead_intake_events_source"), "lead_intake_events", ["source"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_external_lead_id"), "lead_intake_events", ["external_lead_id"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_telegram_username"), "lead_intake_events", ["telegram_username"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_campaign_id"), "lead_intake_events", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_account_id"), "lead_intake_events", ["account_id"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_dialog_id"), "lead_intake_events", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_lead_intake_events_status"), "lead_intake_events", ["status"], unique=False)

    op.create_table(
        "dialog_sequence_runs",
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_intake_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("current_step_position", sa.Integer(), nullable=False),
        sa.Column("awaiting_reply_after_step", sa.Integer(), nullable=True),
        sa.Column("last_inbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("llm_decision_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_inbound_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_intake_event_id"], ["lead_intake_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dialog_sequence_runs_dialog_id"), "dialog_sequence_runs", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_dialog_sequence_runs_campaign_id"), "dialog_sequence_runs", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_dialog_sequence_runs_lead_intake_event_id"), "dialog_sequence_runs", ["lead_intake_event_id"], unique=False)
    op.create_index(op.f("ix_dialog_sequence_runs_status"), "dialog_sequence_runs", ["status"], unique=False)
    op.create_index(op.f("ix_dialog_sequence_runs_last_inbound_message_id"), "dialog_sequence_runs", ["last_inbound_message_id"], unique=False)

    op.create_table(
        "outbound_jobs",
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("campaign_step_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_username", sa.String(length=255), nullable=True),
        sa.Column("peer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_random_id", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_error_type", sa.String(length=100), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_step_id"], ["campaign_steps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sequence_run_id"], ["dialog_sequence_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_outbound_jobs_account_id"), "outbound_jobs", ["account_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_dialog_id"), "outbound_jobs", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_message_id"), "outbound_jobs", ["message_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_campaign_id"), "outbound_jobs", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_sequence_run_id"), "outbound_jobs", ["sequence_run_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_campaign_step_id"), "outbound_jobs", ["campaign_step_id"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_target_username"), "outbound_jobs", ["target_username"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_status"), "outbound_jobs", ["status"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_scheduled_at"), "outbound_jobs", ["scheduled_at"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_next_attempt_at"), "outbound_jobs", ["next_attempt_at"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_lease_owner"), "outbound_jobs", ["lease_owner"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_lease_expires_at"), "outbound_jobs", ["lease_expires_at"], unique=False)
    op.create_index(op.f("ix_outbound_jobs_telegram_random_id"), "outbound_jobs", ["telegram_random_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_outbound_jobs_telegram_random_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_lease_expires_at"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_lease_owner"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_next_attempt_at"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_scheduled_at"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_status"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_target_username"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_campaign_step_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_sequence_run_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_campaign_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_message_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_dialog_id"), table_name="outbound_jobs")
    op.drop_index(op.f("ix_outbound_jobs_account_id"), table_name="outbound_jobs")
    op.drop_table("outbound_jobs")

    op.drop_index(op.f("ix_dialog_sequence_runs_last_inbound_message_id"), table_name="dialog_sequence_runs")
    op.drop_index(op.f("ix_dialog_sequence_runs_status"), table_name="dialog_sequence_runs")
    op.drop_index(op.f("ix_dialog_sequence_runs_lead_intake_event_id"), table_name="dialog_sequence_runs")
    op.drop_index(op.f("ix_dialog_sequence_runs_campaign_id"), table_name="dialog_sequence_runs")
    op.drop_index(op.f("ix_dialog_sequence_runs_dialog_id"), table_name="dialog_sequence_runs")
    op.drop_table("dialog_sequence_runs")

    op.drop_index(op.f("ix_lead_intake_events_status"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_dialog_id"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_account_id"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_campaign_id"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_telegram_username"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_external_lead_id"), table_name="lead_intake_events")
    op.drop_index(op.f("ix_lead_intake_events_source"), table_name="lead_intake_events")
    op.drop_table("lead_intake_events")

    op.drop_index(op.f("ix_campaign_steps_campaign_id"), table_name="campaign_steps")
    op.drop_table("campaign_steps")
    op.drop_index(op.f("ix_campaigns_is_default"), table_name="campaigns")
    op.drop_index(op.f("ix_campaigns_status"), table_name="campaigns")
    op.drop_table("campaigns")

    op.drop_column("accounts", "last_error_message")
    op.drop_column("accounts", "health_status")
    op.drop_column("accounts", "flood_wait_until")
    op.drop_column("accounts", "next_available_at")
    op.drop_column("accounts", "send_jitter_seconds")
    op.drop_column("accounts", "send_interval_seconds")
