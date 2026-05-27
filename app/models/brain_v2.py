from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.vector import Vector
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LeadBrainState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Canonical Brain V2 state for one lead."""

    __tablename__ = "lead_brain_state"

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(80), default="lead_created", index=True, nullable=False)
    current_goal: Mapped[str | None] = mapped_column(Text)
    open_loop: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_candidate_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_candidate_typing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    debounce_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_processed_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="active", index=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class LeadProfileSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Extracted candidate profile slot owned by Brain V2."""

    __tablename__ = "lead_profile_slots"
    __table_args__ = (
        UniqueConstraint("lead_id", "slot_key", name="uq_lead_profile_slots_lead_key"),
    )

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slot_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    slot_value: Mapped[str | None] = mapped_column(Text)
    slot_value_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(50), default="brain", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class LeadAgendaItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One agenda item the dialogue brain should complete."""

    __tablename__ = "lead_agenda_items"
    __table_args__ = (
        UniqueConstraint("lead_id", "item_key", name="uq_lead_agenda_items_lead_key"),
    )

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    item_key: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True, nullable=False)
    completion_rule: Mapped[str] = mapped_column(Text, nullable=False)
    default_question: Mapped[str] = mapped_column(Text, nullable=False)
    slot_key: Mapped[str | None] = mapped_column(String(100), index=True)
    next_stage_hint: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class KnowledgeCard(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """V2 knowledge card used by hybrid retrieval."""

    __tablename__ = "knowledge_cards"

    card_key: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(80), index=True)
    topic: Mapped[str | None] = mapped_column(String(150), index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    triggers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(50), default="approved", index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    embedding = mapped_column(Vector(1536), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class BrainRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Persisted decision trace for one Brain V2 turn."""

    __tablename__ = "brain_runs"

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    incoming_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="shadow_pending", index=True, nullable=False)
    shadow_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stage_before: Mapped[str | None] = mapped_column(String(80), index=True)
    stage_after: Mapped[str | None] = mapped_column(String(80), index=True)
    dialogue_move: Mapped[str | None] = mapped_column(String(100), index=True)
    validator_verdict: Mapped[str | None] = mapped_column(String(50), index=True)
    response_text: Mapped[str | None] = mapped_column(Text)
    message_batch: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    router_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retrieved_card_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    brain_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validator_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    executor_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    state_patch: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


class LLMCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Telemetry for one LLM component call."""

    __tablename__ = "llm_calls"

    brain_run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brain_runs.id", ondelete="SET NULL"), index=True
    )
    component: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(80), index=True)
    model: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(50), default="completed", index=True, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    request_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)


class RetrievalEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Telemetry for one knowledge retrieval pass."""

    __tablename__ = "retrieval_events"

    brain_run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brain_runs.id", ondelete="SET NULL"), index=True
    )
    lead_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    dialog_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="SET NULL"), index=True
    )
    stage: Mapped[str | None] = mapped_column(String(80), index=True)
    query: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    retrieved_card_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
