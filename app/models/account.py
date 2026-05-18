from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """CRMchat / Telegram account connected to the worker."""

    __tablename__ = "accounts"

    crmchat_account_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)

    dialogs: Mapped[list["Dialog"]] = relationship(back_populates="account", cascade="all, delete-orphan")
