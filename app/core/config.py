from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="local", alias="APP_ENV")
    database_url: str = Field(
        default="postgresql+asyncpg://hr_dv_worker:hr_dv_worker@localhost:5432/hr_dv_worker",
        alias="DATABASE_URL",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    crmchat_api_base_url: str | None = Field(default=None, alias="CRMCHAT_API_BASE_URL")
    crmchat_webhook_secret: str | None = Field(default=None, alias="CRMCHAT_WEBHOOK_SECRET")
    llm_provider: str | None = Field(default=None, alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
