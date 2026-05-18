from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AgentActionLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Decision made by the agent for a dialog turn."""

    __tablename__ = "agent_action_logs"

    dialog_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("dialogs.id"), index=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="planned", nullable=False)

    dialog: Mapped["Dialog"] = relationship(back_populates="action_logs")
