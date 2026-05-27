from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LeadFunnelRuntime(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """LangGraph funnel runtime state for one lead."""

    __tablename__ = "lead_funnel_runtime"
    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_lead_funnel_runtime_lead_id"),
        UniqueConstraint("thread_id", name="uq_lead_funnel_runtime_thread_id"),
    )

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    thread_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(80), default="new", index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", index=True, nullable=False)
    last_processed_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    last_candidate_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_candidate_typing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    debounce_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    current_goal: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
