from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class TelegramPollingRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Audit record for one CRMchat Telegram polling cycle."""

    __tablename__ = "telegram_polling_runs"

    crmchat_organization_id: Mapped[str | None] = mapped_column(String(255), index=True)
    crmchat_workspace_id: Mapped[str | None] = mapped_column(String(255), index=True)
    crmchat_account_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(
        String(50), default="started", index=True, nullable=False
    )
    dialogs_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dialogs_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_seen: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    messages_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flood_wait_seconds: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
