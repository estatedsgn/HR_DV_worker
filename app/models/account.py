from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """CRMchat / Telegram account connected to the worker."""

    __tablename__ = "accounts"

    crmchat_account_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    crmchat_organization_id: Mapped[str | None] = mapped_column(String(255), index=True)
    crmchat_workspace_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # Per-account CRMchat connection. When set, this account talks to CRMchat with
    # its OWN base url + bearer key (each Дайвинчик account is a separate CRM
    # connection). When NULL, code falls back to the global .env CRMCHAT_* settings
    # so the original single-account setup keeps working unchanged.
    crmchat_api_base_url: Mapped[str | None] = mapped_column(String(512))
    crmchat_api_key: Mapped[str | None] = mapped_column(Text)
    # Дайвинчик auto-swiper runs per-account, in its own process, with its own
    # "режим" (active hours, like probability, daily cap, bot username) stored as
    # JSON here. daivinchik_enabled gates whether the orchestrator spawns a swiper.
    daivinchik_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    daivinchik_config_json: Mapped[str | None] = mapped_column(Text)
    telegram_username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    send_interval_seconds: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    send_jitter_seconds: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    next_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flood_wait_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    health_status: Mapped[str] = mapped_column(String(50), default="healthy", nullable=False)
    last_error_message: Mapped[str | None] = mapped_column(Text)

    dialogs: Mapped[list["Dialog"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
