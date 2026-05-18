from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Inbound and outbound messages in a dialog."""

    __tablename__ = "messages"

    dialog_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("dialogs.id"), index=True, nullable=False)
    crmchat_message_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    sender_type: Mapped[str] = mapped_column(String(50), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="received", nullable=False)
    sent_at = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    dialog: Mapped["Dialog"] = relationship(back_populates="messages")
    outbound_logs: Mapped[list["OutboundSendLog"]] = relationship(back_populates="message")
