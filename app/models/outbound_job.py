from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class OutboundJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Queued outbound Telegram send with idempotency, leases, and retry metadata."""

    __tablename__ = "outbound_jobs"

    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    sequence_run_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialog_sequence_runs.id", ondelete="SET NULL"), index=True
    )
    campaign_step_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_steps.id", ondelete="SET NULL"), index=True
    )
    target_username: Mapped[str | None] = mapped_column(String(255), index=True)
    peer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    job_type: Mapped[str] = mapped_column(String(50), default="text", index=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_path: Mapped[str | None] = mapped_column(Text)
    media_mime_type: Mapped[str | None] = mapped_column(String(120))
    media_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    typing_action: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="queued", index=True, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    telegram_random_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_error_type: Mapped[str | None] = mapped_column(String(100))
