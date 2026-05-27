from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Lead qualification state produced by the agent."""

    __tablename__ = "leads"

    dialog_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("dialogs.id"), unique=True, index=True, nullable=False)
    qualification_status: Mapped[str] = mapped_column(String(50), default="new", nullable=False)
    funnel_state: Mapped[str] = mapped_column(String(50), default="NEW_LEAD", index=True, nullable=False)
    interest_status: Mapped[str | None] = mapped_column(String(50), index=True)
    score: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    next_step: Mapped[str | None] = mapped_column(String(255))
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    handoff_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lost_reason: Mapped[str | None] = mapped_column(Text)
    do_not_contact_reason: Mapped[str | None] = mapped_column(Text)

    dialog: Mapped["Dialog"] = relationship(back_populates="lead")
    facts: Mapped[list["LeadFact"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
