"""add brain layer tables and lead crm fields

Revision ID: 0010_brain_layer
Revises: 0009_v1_sequences
Create Date: 2026-05-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0010_brain_layer"
down_revision: Union[str, None] = "0009_v1_sequences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


class Vector(sa.types.UserDefinedType):
    cache_ok = True

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **kw) -> str:  # noqa: ANN003
        if self.dimensions:
            return f"vector({self.dimensions})"
        return "vector"


def upgrade() -> None:
    op.add_column(
        "leads",
        sa.Column("funnel_state", sa.String(length=50), server_default="NEW_LEAD", nullable=False),
    )
    op.add_column("leads", sa.Column("interest_status", sa.String(length=50), nullable=True))
    op.add_column("leads", sa.Column("assigned_to", sa.String(length=255), nullable=True))
    op.add_column("leads", sa.Column("handoff_ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("lost_reason", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("do_not_contact_reason", sa.Text(), nullable=True))
    op.create_index(op.f("ix_leads_funnel_state"), "leads", ["funnel_state"], unique=False)
    op.create_index(op.f("ix_leads_interest_status"), "leads", ["interest_status"], unique=False)

    op.create_table(
        "lead_facts",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dialog_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_key", sa.String(length=100), nullable=False),
        sa.Column("fact_value", sa.Text(), nullable=True),
        sa.Column("fact_value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(length=50), server_default="llm", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id", "fact_key", name="uq_lead_facts_lead_key"),
    )
    op.create_index(op.f("ix_lead_facts_dialog_id"), "lead_facts", ["dialog_id"], unique=False)
    op.create_index(op.f("ix_lead_facts_fact_key"), "lead_facts", ["fact_key"], unique=False)
    op.create_index(op.f("ix_lead_facts_lead_id"), "lead_facts", ["lead_id"], unique=False)

    op.create_table(
        "prompt_versions",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="draft", nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
    )
    op.create_index(op.f("ix_prompt_versions_name"), "prompt_versions", ["name"], unique=False)
    op.create_index(op.f("ix_prompt_versions_status"), "prompt_versions", ["status"], unique=False)
    op.create_index(
        "uq_prompt_versions_one_active_per_name",
        "prompt_versions",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_snippets",
        sa.Column("snippet_type", sa.String(length=50), server_default="template", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_snippets_snippet_type"), "knowledge_snippets", ["snippet_type"], unique=False)
    op.create_index(op.f("ix_knowledge_snippets_source"), "knowledge_snippets", ["source"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_knowledge_snippets_source"), table_name="knowledge_snippets")
    op.drop_index(op.f("ix_knowledge_snippets_snippet_type"), table_name="knowledge_snippets")
    op.drop_table("knowledge_snippets")

    op.drop_index("uq_prompt_versions_one_active_per_name", table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_status"), table_name="prompt_versions")
    op.drop_index(op.f("ix_prompt_versions_name"), table_name="prompt_versions")
    op.drop_table("prompt_versions")

    op.drop_index(op.f("ix_lead_facts_lead_id"), table_name="lead_facts")
    op.drop_index(op.f("ix_lead_facts_fact_key"), table_name="lead_facts")
    op.drop_index(op.f("ix_lead_facts_dialog_id"), table_name="lead_facts")
    op.drop_table("lead_facts")

    op.drop_index(op.f("ix_leads_interest_status"), table_name="leads")
    op.drop_index(op.f("ix_leads_funnel_state"), table_name="leads")
    op.drop_column("leads", "do_not_contact_reason")
    op.drop_column("leads", "lost_reason")
    op.drop_column("leads", "handoff_ready_at")
    op.drop_column("leads", "assigned_to")
    op.drop_column("leads", "interest_status")
    op.drop_column("leads", "funnel_state")
