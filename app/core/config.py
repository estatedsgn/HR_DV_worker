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
        default=60.0, alias="CRMCHAT_TIMEOUT_SECONDS"
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
    telegram_mark_read_after_poll: bool = Field(
        default=False, alias="TELEGRAM_MARK_READ_AFTER_POLL"
    )
    llm_provider: str | None = Field(default=None, alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    llm_endpoint: str = Field(default="responses", alias="LLM_ENDPOINT")
    llm_model: str = Field(default="gpt-5.4-mini", alias="LLM_MODEL")
    llm_embedding_model: str = Field(
        default="openai/text-embedding-3-small", alias="LLM_EMBEDDING_MODEL"
    )
    llm_max_input_messages: int = Field(default=20, alias="LLM_MAX_INPUT_MESSAGES")
    llm_max_output_tokens: int = Field(default=300, alias="LLM_MAX_OUTPUT_TOKENS")
    llm_temperature: float = Field(default=0.4, alias="LLM_TEMPERATURE")
    llm_prompt_name: str = Field(default="brain_main", alias="LLM_PROMPT_NAME")
    llm_prompt_version: str = Field(default="v1", alias="LLM_PROMPT_VERSION")
    model_test_profile: str = Field(
        default="plus_max_router", alias="MODEL_TEST_PROFILE"
    )
    qwen_max_model: str = Field(default="qwen3.7-max", alias="QWEN_MAX_MODEL")
    qwen_plus_model: str = Field(default="qwen-plus", alias="QWEN_PLUS_MODEL")
    qwen_flash_model: str = Field(default="qwen-flash", alias="QWEN_FLASH_MODEL")
    llm_mock_decision_json: str = Field(
        default='{"decision":"reply","lead_status":"interested","reply_text":"Спасибо, передам детали менеджеру.","handoff_reason":null,"confidence":0.7}',
        alias="LLM_MOCK_DECISION_JSON",
    )
    brain_min_inbound_before_handoff: int = Field(
        default=0, alias="BRAIN_MIN_INBOUND_BEFORE_HANDOFF"
    )
    brain_inbound_debounce_seconds: int = Field(
        default=3, alias="BRAIN_INBOUND_DEBOUNCE_SECONDS"
    )
    brain_cancel_outbound_on_inbound: bool = Field(
        default=True, alias="BRAIN_CANCEL_OUTBOUND_ON_INBOUND"
    )
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    default_embedding_provider: str = Field(
        default="openai", alias="DEFAULT_EMBEDDING_PROVIDER"
    )
    default_embedding_model: str = Field(
        default="text-embedding-3-small", alias="DEFAULT_EMBEDDING_MODEL"
    )
    brain_v2_enabled: bool = Field(default=False, alias="BRAIN_V2_ENABLED")
    brain_shadow_mode: bool = Field(default=True, alias="BRAIN_SHADOW_MODE")
    brain_enable_validator: bool = Field(default=True, alias="BRAIN_ENABLE_VALIDATOR")
    langgraph_funnel_enabled: bool = Field(default=True, alias="LANGGRAPH_FUNNEL_ENABLED")
    langgraph_checkpoint_postgres_enabled: bool = Field(
        default=True, alias="LANGGRAPH_CHECKPOINT_POSTGRES_ENABLED"
    )
    brain_default_provider: str | None = Field(default=None, alias="BRAIN_DEFAULT_PROVIDER")
    brain_interest_classifier_provider: str | None = Field(
        default=None, alias="BRAIN_INTEREST_CLASSIFIER_PROVIDER"
    )
    brain_interest_classifier_model: str | None = Field(default=None, alias="BRAIN_INTEREST_CLASSIFIER_MODEL")
    brain_router_provider: str | None = Field(default=None, alias="BRAIN_ROUTER_PROVIDER")
    brain_router_model: str | None = Field(default=None, alias="BRAIN_ROUTER_MODEL")
    brain_dialogue_provider: str | None = Field(
        default=None, alias="BRAIN_DIALOGUE_PROVIDER"
    )
    brain_dialogue_model: str | None = Field(default=None, alias="BRAIN_DIALOGUE_MODEL")
    brain_semantic_provider: str | None = Field(default=None, alias="BRAIN_SEMANTIC_PROVIDER")
    brain_semantic_model: str | None = Field(default=None, alias="BRAIN_SEMANTIC_MODEL")
    brain_complex_semantic_provider: str | None = Field(default=None, alias="BRAIN_COMPLEX_SEMANTIC_PROVIDER")
    brain_complex_semantic_model: str | None = Field(default=None, alias="BRAIN_COMPLEX_SEMANTIC_MODEL")
    brain_reply_provider: str | None = Field(default=None, alias="BRAIN_REPLY_PROVIDER")
    brain_reply_model: str | None = Field(default=None, alias="BRAIN_REPLY_MODEL")
    brain_complex_reply_provider: str | None = Field(default=None, alias="BRAIN_COMPLEX_REPLY_PROVIDER")
    brain_complex_reply_model: str | None = Field(default=None, alias="BRAIN_COMPLEX_REPLY_MODEL")
    brain_validator_provider: str | None = Field(
        default=None, alias="BRAIN_VALIDATOR_PROVIDER"
    )
    brain_validator_model: str | None = Field(default=None, alias="BRAIN_VALIDATOR_MODEL")
    brain_handoff_summary_provider: str | None = Field(
        default=None, alias="BRAIN_HANDOFF_SUMMARY_PROVIDER"
    )
    brain_handoff_summary_model: str | None = Field(default=None, alias="BRAIN_HANDOFF_SUMMARY_MODEL")
    brain_llm_temperature: float = Field(default=0.2, alias="BRAIN_LLM_TEMPERATURE")
    brain_llm_timeout_seconds: float = Field(default=30.0, alias="BRAIN_LLM_TIMEOUT_SECONDS")
    brain_llm_max_retries: int = Field(default=2, alias="BRAIN_LLM_MAX_RETRIES")
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
    outbound_mark_read_on_send: bool = Field(
        default=True, alias="OUTBOUND_MARK_READ_ON_SEND"
    )
    outbound_typing_delay_seconds: float = Field(
        default=10.0, alias="OUTBOUND_TYPING_DELAY_SECONDS"
    )
    outbound_typing_min_delay_seconds: float = Field(
        default=5.0, alias="OUTBOUND_TYPING_MIN_DELAY_SECONDS"
    )
    outbound_voice_recording_delay_seconds: float = Field(
        default=40.0, alias="OUTBOUND_VOICE_RECORDING_DELAY_SECONDS"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
