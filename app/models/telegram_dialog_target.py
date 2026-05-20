from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class TelegramDialogTarget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "telegram_dialog_targets"

    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    telegram_peer_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    telegram_peer_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500))
