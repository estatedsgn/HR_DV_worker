from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class DialogSequenceRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """State machine instance that drives one dialog through a campaign."""

    __tablename__ = "dialog_sequence_runs"

    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    lead_intake_event_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lead_intake_events.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="active", index=True, nullable=False)
    current_step_position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    awaiting_reply_after_step: Mapped[int | None] = mapped_column(Integer)
    last_inbound_message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    llm_decision_json: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
