from __future__ import annotations

from sqlalchemy import Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LeadFact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Structured fact extracted from a lead conversation."""

    __tablename__ = "lead_facts"
    __table_args__ = (
        UniqueConstraint("lead_id", "fact_key", name="uq_lead_facts_lead_key"),
    )

    lead_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True, nullable=False
    )
    dialog_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dialogs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    fact_value: Mapped[str | None] = mapped_column(Text)
    fact_value_json: Mapped[dict | None] = mapped_column(JSONB)
    source: Mapped[str] = mapped_column(String(50), default="llm", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    lead: Mapped["Lead"] = relationship(back_populates="facts")
