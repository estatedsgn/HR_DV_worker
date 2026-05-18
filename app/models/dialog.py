from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Dialog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Conversation with a lead."""

    __tablename__ = "dialogs"

    account_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), index=True, nullable=False)
    crmchat_dialog_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    lead_external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(50), default="open", nullable=False)
    memory_summary: Mapped[str | None] = mapped_column(Text)

    account: Mapped["Account"] = relationship(back_populates="dialogs")
    messages: Mapped[list["Message"]] = relationship(back_populates="dialog", cascade="all, delete-orphan")
    lead: Mapped["Lead | None"] = relationship(back_populates="dialog", cascade="all, delete-orphan")
    action_logs: Mapped[list["AgentActionLog"]] = relationship(back_populates="dialog", cascade="all, delete-orphan")
    outbound_logs: Mapped[list["OutboundSendLog"]] = relationship(back_populates="dialog", cascade="all, delete-orphan")
    handoffs: Mapped[list["HumanHandoff"]] = relationship(back_populates="dialog", cascade="all, delete-orphan")
