from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class InboundEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Unified inbound event queue for webhook/polling sources."""

    __tablename__ = "inbound_events"

    source: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    external_event_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    external_message_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        index=True,
    )
    dialog_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dialogs.id", ondelete="SET NULL"),
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), default="received", index=True, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_error_type: Mapped[str | None] = mapped_column(String(100))
