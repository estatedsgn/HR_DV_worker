from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LeadIntakeEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Normalized inbound lead request before it becomes a dialog sequence."""

    __tablename__ = "lead_intake_events"
    __table_args__ = (
        UniqueConstraint("source", "external_lead_id", name="uq_lead_intake_source_external"),
    )

    source: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    external_lead_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    telegram_username: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    campaign_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    dialog_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(50), default="received", index=True, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
