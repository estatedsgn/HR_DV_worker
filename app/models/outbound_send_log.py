from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class OutboundSendLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Attempt to send a message plus anti-ban throttling metadata."""

    __tablename__ = "outbound_send_logs"

    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id"), index=True, nullable=False
    )
    message_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    rate_limit_bucket: Mapped[str | None] = mapped_column(String(255))
    telegram_random_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    scheduled_at = mapped_column(DateTime(timezone=True))
    sent_at = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    dialog: Mapped["Dialog"] = relationship(back_populates="outbound_logs")
    message: Mapped["Message | None"] = relationship(back_populates="outbound_logs")
