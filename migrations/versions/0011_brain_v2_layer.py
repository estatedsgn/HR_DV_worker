"""add brain v2 layer tables

Revision ID: 0011_brain_v2_layer
Revises: 0010_brain_layer
Create Date: 2026-05-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0011_brain_v2_layer"
down_revision: Union[str, None] = "0010_brain_layer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class Vector(sa.types.UserDefinedType):
    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:  # noqa: ANN003
        return f"vector({self.dimensions})" if self.dimensions else "vector"


def timestamp_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "lead_brain_state",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.String(length=80), server_default="lead_created", nullable=False),
        sa.Column("current_goal", sa.Text(), nullable=True),
        sa.Column("open_loop", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="active", nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", name="uq_lead_brain_state_lead_id"),
    )
    op.create_index(op.f("ix_lead_brain_state_dialog_id"), "lead_brain_state", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_lead_brain_state_lead_id"), "lead_brain_state", ["lead_id"], unique=True)
    op.create_index(op.f("ix_lead_brain_state_stage"), "lead_brain_state", ["stage"], unique=False)
    op.create_index(op.f("ix_lead_brain_state_status"), "lead_brain_state", ["status"], unique=False)

    op.create_table(
        "lead_profile_slots",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slot_key", sa.String(length=100), nullable=False),
        sa.Column("slot_value", sa.Text(), nullable=True),
        sa.Column("slot_value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(length=50), server_default="brain", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", "slot_key", name="uq_lead_profile_slots_lead_key"),
    )
    op.create_index(op.f("ix_lead_profile_slots_dialog_id"), "lead_profile_slots", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_lead_profile_slots_lead_id"), "lead_profile_slots", ["lead_id"], unique=False)
    op.create_index(op.f("ix_lead_profile_slots_slot_key"), "lead_profile_slots", ["slot_key"], unique=False)

    op.create_table(
        "lead_agenda_items",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_key", sa.String(length=150), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("completion_rule", sa.Text(), nullable=False),
        sa.Column("default_question", sa.Text(), nullable=False),
        sa.Column("slot_key", sa.String(length=100), nullable=True),
        sa.Column("next_stage_hint", sa.String(length=80), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", "item_key", name="uq_lead_agenda_items_lead_key"),
    )
    op.create_index(op.f("ix_lead_agenda_items_dialog_id"), "lead_agenda_items", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_lead_agenda_items_item_key"), "lead_agenda_items", ["item_key"], unique=False)
    op.create_index(op.f("ix_lead_agenda_items_lead_id"), "lead_agenda_items", ["lead_id"], unique=False)
    op.create_index(op.f("ix_lead_agenda_items_slot_key"), "lead_agenda_items", ["slot_key"], unique=False)
    op.create_index(op.f("ix_lead_agenda_items_stage"), "lead_agenda_items", ["stage"], unique=False)
    op.create_index(op.f("ix_lead_agenda_items_status"), "lead_agenda_items", ["status"], unique=False)

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_cards",
        sa.Column("card_key", sa.String(length=150), nullable=False),
        sa.Column("stage", sa.String(length=80), nullable=True),
        sa.Column("topic", sa.String(length=150), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("triggers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("verification_status", sa.String(length=50), server_default="approved", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        id_column(),
        *timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("card_key", name="uq_knowledge_cards_card_key"),
    )
    op.create_index(op.f("ix_knowledge_cards_active"), "knowledge_cards", ["active"], unique=False)
    op.create_index(op.f("ix_knowledge_cards_card_key"), "knowledge_cards", ["card_key"], unique=True)
    op.create_index(op.f("ix_knowledge_cards_stage"), "knowledge_cards", ["stage"], unique=False)
    op.create_index(op.f("ix_knowledge_cards_topic"), "knowledge_cards", ["topic"], unique=False)
    op.create_index(op.f("ix_knowledge_cards_verification_status"), "knowledge_cards", ["verification_status"], unique=False)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_cards_embedding_hnsw "
        "ON knowledge_cards USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "brain_runs",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incoming_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="shadow_pending", nullable=False),
        sa.Column("shadow_mode", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("stage_before", sa.String(length=80), nullable=True),
        sa.Column("stage_after", sa.String(length=80), nullable=True),
        sa.Column("dialogue_move", sa.String(length=100), nullable=True),
        sa.Column("validator_verdict", sa.String(length=50), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("router_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("retrieved_card_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("brain_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("validator_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("executor_action", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state_patch", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incoming_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ["dialog_id", "dialogue_move", "incoming_message_id", "lead_id", "stage_after", "stage_before", "status", "validator_verdict"]:
        op.create_index(op.f(f"ix_brain_runs_{column}"), "brain_runs", [column], unique=False)

    op.create_table(
        "llm_calls",
        sa.Column("brain_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("component", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="completed", nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("request_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["brain_run_id"], ["brain_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ["brain_run_id", "component", "model", "provider", "status"]:
        op.create_index(op.f(f"ix_llm_calls_{column}"), "llm_calls", [column], unique=False)

    op.create_table(
        "retrieval_events",
        sa.Column("brain_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stage", sa.String(length=80), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("topics", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("retrieved_card_ids", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        id_column(),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(["brain_run_id"], ["brain_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ["brain_run_id", "dialog_id", "lead_id", "stage"]:
        op.create_index(op.f(f"ix_retrieval_events_{column}"), "retrieval_events", [column], unique=False)


def downgrade() -> None:
    for table, columns in [
        ("retrieval_events", ["brain_run_id", "dialog_id", "lead_id", "stage"]),
        ("llm_calls", ["brain_run_id", "component", "model", "provider", "status"]),
        ("brain_runs", ["dialog_id", "dialogue_move", "incoming_message_id", "lead_id", "stage_after", "stage_before", "status", "validator_verdict"]),
    ]:
        for column in columns:
            op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
    op.drop_table("retrieval_events")
    op.drop_table("llm_calls")
    op.drop_table("brain_runs")

    op.execute("DROP INDEX IF EXISTS ix_knowledge_cards_embedding_hnsw")
    op.drop_index(op.f("ix_knowledge_cards_verification_status"), table_name="knowledge_cards")
    op.drop_index(op.f("ix_knowledge_cards_topic"), table_name="knowledge_cards")
    op.drop_index(op.f("ix_knowledge_cards_stage"), table_name="knowledge_cards")
    op.drop_index(op.f("ix_knowledge_cards_card_key"), table_name="knowledge_cards")
    op.drop_index(op.f("ix_knowledge_cards_active"), table_name="knowledge_cards")
    op.drop_table("knowledge_cards")

    for column in ["dialog_id", "item_key", "lead_id", "slot_key", "stage", "status"]:
        op.drop_index(op.f(f"ix_lead_agenda_items_{column}"), table_name="lead_agenda_items")
    op.drop_table("lead_agenda_items")

    for column in ["dialog_id", "lead_id", "slot_key"]:
        op.drop_index(op.f(f"ix_lead_profile_slots_{column}"), table_name="lead_profile_slots")
    op.drop_table("lead_profile_slots")

    for column in ["dialog_id", "lead_id", "stage", "status"]:
        op.drop_index(op.f(f"ix_lead_brain_state_{column}"), table_name="lead_brain_state")
    op.drop_table("lead_brain_state")
