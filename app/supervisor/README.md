# Пульт управления агентом (supervisor)

Запуск/остановка агента кнопками в Telegram — без ручного дёрганья скриптов.

## Как запустить

1. Убедись, что в `.env` заданы `CONTROL_BOT_TOKEN` (бот @HrAgentControlbot уже вписан)
   и рабочие ключи `CRMCHAT_API_KEY`, `LLM_API_KEY`.
2. Двойной клик по **`start_supervisor.bat`** в корне проекта
   (или `\.venv\Scripts\python.exe -m app.supervisor`).
3. Открой бота в Telegram, нажми **/start**. Бот пришлёт твой `chat_id` — впиши его
   в `.env` как `CONTROL_ADMIN_CHAT_ID=...`, чтобы привязка сохранилась между перезапусками.

## Кнопки

| Кнопка | Что делает |
|--------|-----------|
| ▶️ Старт | Поднимает Docker+Postgres → миграции → проверяет CRMChat → запускает автопилот. О каждом шаге пишет в чат. При ошибке шага агент НЕ стартует и присылает причину. |
| ⏹ Стоп | Мягко останавливает автопилот. Docker/Postgres остаются поднятыми. |
| 📊 Статус | Состояние, аптайм, PID, цель, последняя ошибка. |
| 📜 Логи | Последние строки автопилота. |

Команды-дублёры: `/start` `/menu` `/status` `/stop` `/logs`.

Агент работает, пока не нажмёшь ⏹ Стоп (или не закроешь пульт). Если автопилот
неожиданно падает сам — бот пришлёт алерт с хвостом лога.

## Архитектура (для переноса на VPS)

- **`supervisor.py`** — ядро (машина состояний), транспортно-независимо.
- **`infra.py`** — шаги подъёма инфры как сменяемый адаптер. Каждый шаг шелится
  наружу (docker / alembic / `scripts/sync_crmchat_accounts.py`), поэтому ядро
  не тянет asyncpg в свой процесс. На VPS: подмени `LocalInfraProvider` своим
  провайдером или отключи шаги через `CONTROL_SKIP_DOCKER=1` / `CONTROL_SKIP_MIGRATIONS=1`.
- **`control/`** — пульты-адаптеры. Сейчас `TelegramControlAdapter`; интерфейс
  `ControlAdapter` позволяет добавить web/CLI, не трогая ядро.
- **`process.py`** — управление дочерним процессом автопилота (стрим логов,
  мягкая остановка CTRL_BREAK/SIGTERM).

На VPS код тот же: `python -m app.supervisor`. Лог автопилота — `runtime_logs/supervisor_autopilot.log`.

## Настройки (`.env`)

| Переменная | Назначение | Дефолт |
|-----------|-----------|--------|
| `CONTROL_BOT_TOKEN` | токен бота-пульта | — |
| `CONTROL_ADMIN_CHAT_ID` | кому отвечать (привязка владельца) | пусто = первый написавший |
| `CONTROL_TARGET_USERNAME` | кого ведёт автопилот (`--only-username`) | `@iamnekiy` |
| `CONTROL_AUTOPILOT_ARGS` | аргументы `run_autopilot.py` | `--all-accounts --allow-real-send --typing-delay-seconds 2 --poll-interval-seconds 3` |
| `CONTROL_SKIP_DOCKER` | пропустить шаг Docker (если Postgres снаружи) | `false` |
| `CONTROL_SKIP_MIGRATIONS` | пропустить alembic | `false` |
