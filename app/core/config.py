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
    # How long to back an account off after a polling cycle looks throttled
    # (saw dialogs, synced none, several timed out). During the window polling
    # and the swiper skip the account so Telegram's flood can clear.
    telegram_throttle_backoff_seconds: int = Field(
        default=300, alias="TELEGRAM_THROTTLE_BACKOFF_SECONDS"
    )
    telegram_poll_history_limit: int = Field(
        default=20, alias="TELEGRAM_POLL_HISTORY_LIMIT"
    )
    telegram_mark_read_after_poll: bool = Field(
        default=False, alias="TELEGRAM_MARK_READ_AFTER_POLL"
    )
    llm_provider: str | None = Field(default=None, alias="LLM_PROVIDER")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    # Pool of interchangeable keys for the same provider (comma/whitespace
    # separated). When the active key's quota is exhausted the runtime rotates
    # to the next one automatically. LLM_API_KEY is appended as a fallback so
    # existing single-key setups keep working unchanged.
    llm_api_keys: str | None = Field(default=None, alias="LLM_API_KEYS")
    # Shared cursor file so every worker process agrees on the active key.
    llm_key_state_path: str = Field(
        default="runtime_logs/llm_key_cursor.json", alias="LLM_KEY_STATE_PATH"
    )
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
    # A/B по аккаунтам: какой профиль модели использовать на каждый аккаунт и как
    # его подписывать в транскриптах. Формат "id=value,id=value" (id — UUID
    # аккаунта или его crmchat_account_id). Пусто → общий MODEL_TEST_PROFILE.
    account_model_profiles: str | None = Field(default=None, alias="ACCOUNT_MODEL_PROFILES")
    account_labels: str | None = Field(default=None, alias="ACCOUNT_LABELS")
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
    # Повторные касания замолчавших лидов: до 3 «бампов» с нарастающими паузами
    # (часы, через запятую), потом замолкаем навсегда. См. funnel_graph/reengage.py.
    reengage_enabled: bool = Field(default=True, alias="REENGAGE_ENABLED")
    reengage_touch_delays_hours: str = Field(
        default="4,20,48", alias="REENGAGE_TOUCH_DELAYS_HOURS"
    )
    # Максимум бампов за один скан — чтобы открытие рабочего окна не давало
    # массовую волну одинаковых сообщений со всех аккаунтов разом.
    reengage_batch_limit: int = Field(default=10, alias="REENGAGE_BATCH_LIMIT")
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
    # Источники интейка, чьи лиды разрешено вести аутричем. Диалог такого лида
    # имеет crmchat_dialog_id вида "intake:<source>:<id>". Только эти диалоги
    # (плюс явный username-allowlist) получают исходящие — обычные telegram:-диалоги
    # (боты/случайные, с кем аккаунт переписывается) НЕ трогаются.
    outbound_allowed_intake_sources: str = Field(
        default="daivinchik", alias="OUTBOUND_ALLOWED_INTAKE_SOURCES"
    )
    # Анти-флуд пейсинг между исходящими с одного аккаунта. Раньше было 300/60 c —
    # это создавало 5-минутную стену между «пузырями» одного ответа и между
    # репликами внутри живого диалога (next_available_at гейтит каждый следующий
    # job). Реальная отправка + имитация печати и так дают естественную паузу,
    # поэтому держим интервал маленьким. Холодный first-touch всё ещё ограничен
    # outbound-гейтом (только daivinchik-лиды / allowlist), а не таймером.
    outbound_default_send_interval_seconds: int = Field(
        default=3, alias="OUTBOUND_DEFAULT_SEND_INTERVAL_SECONDS"
    )
    outbound_default_send_jitter_seconds: int = Field(
        default=3, alias="OUTBOUND_DEFAULT_SEND_JITTER_SECONDS"
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
    # Минимальная пауза между ДВУМЯ отправками в ОДИН диалог (анти-залп):
    # per-account интервал выше не мешает выстрелить 4 сообщения одного ответа
    # одним батчем. Джоба, попавшая под гейт, сдвигается без расхода attempt.
    outbound_min_dialog_gap_seconds: float = Field(
        default=5.0, alias="OUTBOUND_MIN_DIALOG_GAP_SECONDS"
    )

    # --- Supervisor / remote control pult ---
    control_bot_token: str | None = Field(default=None, alias="CONTROL_BOT_TOKEN")
    control_admin_chat_id: str | None = Field(
        default=None, alias="CONTROL_ADMIN_CHAT_ID"
    )
    control_target_username: str | None = Field(
        default="@iamnekiy", alias="CONTROL_TARGET_USERNAME"
    )
    control_autopilot_args: str = Field(
        default="--all-accounts --allow-real-send --typing-delay-seconds 2 --poll-interval-seconds 3",
        alias="CONTROL_AUTOPILOT_ARGS",
    )
    # Когда True, пульт сам поднимает агента при старте процесса (после ребута /
    # рестарта systemd) — не дожидаясь ручного ▶️ Старт в Telegram. Вместе с
    # авто-рестартом автопилота это делает агента самовосстанавливающимся 24/7.
    control_autostart: bool = Field(default=False, alias="CONTROL_AUTOSTART")
    control_db_health_retries: int = Field(
        default=30, alias="CONTROL_DB_HEALTH_RETRIES"
    )
    lead_alerts_enabled: bool = Field(default=True, alias="LEAD_ALERTS_ENABLED")
    # Hand-off alerts go to a dedicated bot/chat when set, otherwise they fall
    # back to the supervisor control bot (control_bot_token / control_admin_chat_id).
    handoff_bot_token: str | None = Field(default=None, alias="HANDOFF_BOT_TOKEN")
    handoff_chat_id: str | None = Field(default=None, alias="HANDOFF_CHAT_ID")
    control_skip_docker: bool = Field(default=False, alias="CONTROL_SKIP_DOCKER")
    control_skip_migrations: bool = Field(
        default=False, alias="CONTROL_SKIP_MIGRATIONS"
    )

    # --- Дайвинчик auto-swiper ---
    daivinchik_enabled: bool = Field(default=False, alias="DAIVINCHIK_ENABLED")
    daivinchik_bot_username: str = Field(
        default="@leomatchbot", alias="DAIVINCHIK_BOT_USERNAME"
    )
    daivinchik_timezone: str = Field(
        default="Europe/Moscow", alias="DAIVINCHIK_TIMEZONE"
    )
    daivinchik_active_hours_start: int = Field(
        default=10, alias="DAIVINCHIK_ACTIVE_HOURS_START"
    )
    daivinchik_active_hours_end: int = Field(
        default=21, alias="DAIVINCHIK_ACTIVE_HOURS_END"
    )
    daivinchik_poll_interval_seconds: float = Field(
        default=5.0, alias="DAIVINCHIK_POLL_INTERVAL_SECONDS"
    )
    daivinchik_min_action_delay_seconds: float = Field(
        default=30.0, alias="DAIVINCHIK_MIN_ACTION_DELAY_SECONDS"
    )
    daivinchik_max_action_delay_seconds: float = Field(
        default=60.0, alias="DAIVINCHIK_MAX_ACTION_DELAY_SECONDS"
    )
    daivinchik_like_probability: float = Field(
        default=0.5, alias="DAIVINCHIK_LIKE_PROBABILITY"
    )
    daivinchik_daily_lead_limit: int = Field(
        default=7, alias="DAIVINCHIK_DAILY_LEAD_LIMIT"
    )
    daivinchik_limit_pause_minutes: int = Field(
        default=90, alias="DAIVINCHIK_LIMIT_PAUSE_MINUTES"
    )
    daivinchik_ad_button_from_right: int = Field(
        default=2, alias="DAIVINCHIK_AD_BUTTON_FROM_RIGHT"
    )
    daivinchik_history_limit: int = Field(
        default=40, alias="DAIVINCHIK_HISTORY_LIMIT"
    )
    # Сколько страниц истории листать назад за тик, чтобы покрыть пачку сообщений
    # Дайвинчика (открытые «взаимные симпатии» вываливают десятки карточек разом).
    # 40 * 8 = до 320 сообщений с момента последнего курсора — с большим запасом.
    daivinchik_history_max_pages: int = Field(
        default=8, alias="DAIVINCHIK_HISTORY_MAX_PAGES"
    )
    daivinchik_state_path: str = Field(
        default="daivinchik_state.json", alias="DAIVINCHIK_STATE_PATH"
    )
    daivinchik_review_log_path: str = Field(
        default="daivinchik_review.jsonl", alias="DAIVINCHIK_REVIEW_LOG_PATH"
    )
    daivinchik_leads_path: str = Field(
        default="daivinchik_leads.jsonl", alias="DAIVINCHIK_LEADS_PATH"
    )
    daivinchik_lead_source: str = Field(
        default="daivinchik", alias="DAIVINCHIK_LEAD_SOURCE"
    )


    @staticmethod
    def _parse_account_map(raw: str | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for pair in (raw or "").replace("\n", ",").split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if key and value:
                result[key] = value
        return result

    def model_profile_for_account(self, *account_keys: str | None) -> str | None:
        """Профиль модели для аккаунта по любому из его ключей (UUID / crmchat id)."""
        mapping = self._parse_account_map(self.account_model_profiles)
        for key in account_keys:
            if key and str(key) in mapping:
                return mapping[str(key)]
        return None

    def label_for_account(self, *account_keys: str | None) -> str | None:
        mapping = self._parse_account_map(self.account_labels)
        for key in account_keys:
            if key and str(key) in mapping:
                return mapping[str(key)]
        return None

    def llm_api_key_pool(self) -> list[str]:
        """All interchangeable LLM keys in priority order, de-duplicated.

        Combines ``LLM_API_KEYS`` (comma/whitespace separated) with the legacy
        single ``LLM_API_KEY`` appended last as a fallback.
        """
        raw: list[str] = []
        if self.llm_api_keys:
            raw.extend(self.llm_api_keys.replace("\n", ",").replace(" ", ",").split(","))
        if self.llm_api_key:
            raw.append(self.llm_api_key)
        seen: set[str] = set()
        keys: list[str] = []
        for item in raw:
            key = item.strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
