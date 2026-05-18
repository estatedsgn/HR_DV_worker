from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class HumanHandoff(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Escalation of a dialog from the agent to a human operator."""

    __tablename__ = "human_handoffs"

    dialog_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("dialogs.id"), index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="open", nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    resolved_at = mapped_column(DateTime(timezone=True))

    dialog: Mapped["Dialog"] = relationship(back_populates="handoffs")
