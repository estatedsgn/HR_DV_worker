from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = Field(default="local", alias="APP_ENV")
    database_url: str = Field(
        default="postgresql+asyncpg://hr_dv_worker:hr_dv_worker@localhost:5432/hr_dv_worker",
        alias="DATABASE_URL",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    crmchat_api_base_url: str | None = Field(default=None, alias="CRMCHAT_API_BASE_URL")
    crmchat_api_key: str | None = Field(default=None, alias="CRMCHAT_API_KEY")
    crmchat_webhook_secret: str | None = Field(
        default=None, alias="CRMCHAT_WEBHOOK_SECRET"
    )
    crmchat_organization_id: str | None = Field(
        default=None, alias="CRMCHAT_ORGANIZATION_ID"
    )
    crmchat_workspace_id: str | None = Field(default=None, alias="CRMCHAT_WORKSPACE_ID")
    crmchat_default_telegram_account_id: str | None = Field(
        default=None, alias="CRMCHAT_DEFAULT_TELEGRAM_ACCOUNT_ID"
    )
    crmchat_timeout_seconds: float = Field(
        default=10.0, alias="CRMCHAT_TIMEOUT_SECONDS"
    )
    telegram_poll_interval_seconds: int = Field(
        default=60, alias="TELEGRAM_POLL_INTERVAL_SECONDS"
    )
    telegram_poll_dialogs_limit: int = Field(
        default=20, alias="TELEGRAM_POLL_DIALOGS_LIMIT"
    )
    telegram_poll_history_limit: int = Field(
        default=20, alias="TELEGRAM_POLL_HISTORY_LIMIT"
    )
    llm_provider: str | None = Field(default=None, alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str = Field(default="gpt-5.4-mini", alias="LLM_MODEL")
    llm_mock_decision_json: str = Field(
        default='{"decision":"reply","lead_status":"interested","reply_text":"Спасибо, передам детали менеджеру.","handoff_reason":null,"confidence":0.7}',
        alias="LLM_MOCK_DECISION_JSON",
    )
    outbound_real_send_enabled: bool = Field(
        default=False, alias="OUTBOUND_REAL_SEND_ENABLED"
    )
    outbound_allowed_usernames: str = Field(
        default="@iamnekiy", alias="OUTBOUND_ALLOWED_USERNAMES"
    )
    outbound_default_send_interval_seconds: int = Field(
        default=300, alias="OUTBOUND_DEFAULT_SEND_INTERVAL_SECONDS"
    )
    outbound_default_send_jitter_seconds: int = Field(
        default=60, alias="OUTBOUND_DEFAULT_SEND_JITTER_SECONDS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
