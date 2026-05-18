# HR DV Worker

HR DV Worker is the first-stage scaffold for an async Python service that will process CRMchat / Telegram lead dialogs, persist conversation state, qualify leads, and coordinate automated or human responses.

## Current scope

The repository currently contains a minimal FastAPI application, PostgreSQL persistence skeleton, Alembic migrations, structured logging, and placeholder service interfaces for future integrations.

### Healthcheck

```http
GET /health
```

Returns:

```json
{ "status": "ok" }
```

## Architecture: stage 1

### CRMchat connector

`CRMChatConnector` is the future boundary for CRMchat webhook parsing and REST API calls. In this stage it only exposes placeholder methods, keeping external CRMchat details isolated from the worker pipeline.

### Sendler with anti-ban limitations

`Sendler` is the outbound delivery interface. It will later own throttling, pacing, retry, rate-limit buckets, and anti-ban rules before sending messages back to CRMchat / Telegram. Send attempts are designed to be audited through `OutboundSendLog`.

### LLM adapter

`LLMAdapter` defines a provider-neutral interface for future model calls. Configuration already includes placeholders for `LLM_PROVIDER` and `LLM_API_KEY`, so provider-specific clients can be added without changing agent orchestration code.

### DB, repositories, and logging

The persistence layer uses SQLAlchemy async sessions against PostgreSQL. The initial ORM model set covers:

- `Account` — connected CRMchat / Telegram account.
- `Dialog` — conversation with a lead.
- `Message` — inbound and outbound dialog messages.
- `Lead` — qualification status and next-step metadata.
- `AgentActionLog` — agent decisions.
- `OutboundSendLog` — outbound attempts, retry details, and rate-limit metadata.
- `HumanHandoff` — escalation to a human operator.

Repository classes provide small async access layers for accounts, dialogs, messages, leads, and logs. Logging is structured as key-value records with timestamp, level, logger name, message, and optional `account_id`, `dialog_id`, and `message_id` context fields.

### Agent worker

`AgentWorker` documents the future message-processing pipeline:

1. Accept an incoming message.
2. Save the message.
3. Update dialog history and memory.
4. Qualify the lead.
5. Choose the next action.
6. Hand off to a human when required.
7. Send a response through `Sendler` when needed.
8. Log the result.

At this stage the worker is intentionally a scaffold and does not perform real integrations.

### Human handoff

`HumanHandoffService` is the future policy and workflow boundary for escalating conversations to a human operator. Handoff records are persisted with reason, status, assignee, notes, and resolution time.

## Configuration

Copy `.env.example` to `.env` for local development:

```bash
cp .env.example .env
```

Available settings:

- `APP_ENV`
- `DATABASE_URL`
- `LOG_LEVEL`
- `CRMCHAT_API_BASE_URL`
- `CRMCHAT_WEBHOOK_SECRET`
- `LLM_PROVIDER`
- `LLM_API_KEY`

## Local development

Start PostgreSQL:

```bash
docker compose up -d postgres
```

Run migrations:

```bash
alembic upgrade head
```

Run the API:

```bash
uvicorn app.main:app --reload
```

## CRMchat connector scaffold

The CRMchat integration is isolated behind `CRMChatConnector`. The connector is prepared for real API calls, but tests should inject a mock `httpx.AsyncClient` so no external network is required during development.

Prepared connector capabilities:

- Bearer-token CRMchat REST client configuration from environment variables.
- Bootstrap helpers for organizations, workspaces, and active Telegram accounts.
- A local allowlist for Telegram Raw API methods before forwarding calls to CRMchat.
- A universal Telegram Raw API method caller for `/v1/workspaces/{workspaceId}/telegram-accounts/{accountId}/call/{method}`.
- Thin wrappers for `contacts.resolveUsername`, `contacts.search`, `messages.getDialogs`, `messages.getHistory`, `messages.readHistory`, and `messages.sendMessage`.
- `FLOOD_WAIT_N` parsing into a typed `TelegramFloodWaitError` for future Sendler retry scheduling.
- Webhook signature verification with HMAC-SHA256 using `CRMCHAT_WEBHOOK_SECRET`.

Additional CRMchat settings:

- `CRMCHAT_API_BASE_URL` — CRMchat API server URL, usually `https://api.crmchat.ai`.
- `CRMCHAT_API_KEY` — bearer token for API calls; keep it only in local/secret environment storage.
- `CRMCHAT_ORGANIZATION_ID` — optional explicit organization selection.
- `CRMCHAT_WORKSPACE_ID` — optional explicit workspace selection.
- `CRMCHAT_DEFAULT_TELEGRAM_ACCOUNT_ID` — optional explicit Telegram account selection.
- `CRMCHAT_TIMEOUT_SECONDS` — HTTP timeout for CRMchat requests.

Telegram peer metadata is stored on dialogs so future outbound messages can use the correct `InputPeer` data. Outbound send logs also have `telegram_random_id` for Telegram `messages.sendMessage` idempotency.

## CRMchat webhooks

The service exposes a CRMchat webhook receiver at:

```http
POST /webhooks/crmchat
```

The receiver verifies `X-Webhook-Signature` with `CRMCHAT_WEBHOOK_SECRET`, parses the CRMchat webhook envelope, stores the event in `crmchat_webhook_events`, and returns an idempotent response for repeated `eventId` values.

Supported CRM contact events are stored with `status=received`:

- `contact.created`
- `contact.updated`
- `contact.deleted`

Other event types are stored with `status=ignored` so we can inspect future CRMchat payloads without breaking delivery retries.

For local CRMchat webhook testing:

1. Run the API:

   ```bash
   uvicorn app.main:app --reload
   ```

2. Expose it through a public tunnel, for example:

   ```bash
   ngrok http 8000
   ```

3. Register the public URL in CRMchat:

   ```text
   https://<ngrok-domain>/webhooks/crmchat
   ```

4. Configure the same signing secret in CRMchat and in `.env` as `CRMCHAT_WEBHOOK_SECRET`.

Contact webhooks are useful for CRM contact synchronization, but they are not enough by themselves for an auto-reply Telegram agent. If CRMchat does not provide message webhooks, inbound Telegram messages should be ingested through polling with `messages.getDialogs` and `messages.getHistory`.

## Telegram polling sync

CRMchat contact webhooks do not provide a full Telegram message stream. Until a message webhook is available, the worker can read recent Telegram activity through CRMchat Telegram Raw API polling.

Default polling settings:

- `TELEGRAM_POLL_INTERVAL_SECONDS=60` — recommended cadence is one request cycle per minute.
- `TELEGRAM_POLL_DIALOGS_LIMIT=20` — maximum recent dialogs requested per cycle.
- `TELEGRAM_POLL_HISTORY_LIMIT=20` — maximum recent messages requested per dialog.

Run one safe read-only polling cycle and persist new dialogs/messages:

```bash
python scripts/poll_telegram_updates.py
```

Run continuous polling:

```bash
python scripts/poll_telegram_updates.py --loop
```

The polling service stores each run in `telegram_polling_runs`, creates/updates `Account` and `Dialog` records, and inserts new `Message` rows with stable external ids. New user dialogs start with `status=pending_review`; non-user peers are marked `ignored`. This keeps noisy/warmup/non-target conversations out of the agent pipeline until lead qualification is implemented.

If Telegram returns `FLOOD_WAIT_N`, the service records `status=rate_limited`, stores `flood_wait_seconds`, calculates `next_run_at`, and waits at least the larger value of `N` or `TELEGRAM_POLL_INTERVAL_SECONDS` before the next loop iteration.

## Verifying CRMchat API credentials

Use the read-only probe script after filling CRMchat settings in `.env`:

```bash
python scripts/crmchat_probe.py --dialogs-limit 5
```

The probe performs safe read-only checks only:

1. Bootstraps the selected organization, workspace, and active Telegram account.
2. Lists workspace/account counts for the selected context.
3. Calls `messages.getDialogs` through CRMchat Telegram Raw API.
4. Prints normalized dialog summaries without printing Telegram `accessHash` values.

To inspect recent history for the first normalized dialog, run:

```bash
python scripts/crmchat_probe.py --dialogs-limit 5 --with-history --history-limit 5
```

To save a shareable diagnostic snapshot, use a local redacted file path:

```bash
python scripts/crmchat_probe.py --dialogs-limit 5 --save-redacted docs/samples/crmchat_probe.local.json
```

Files matching `docs/samples/*.local.json` are ignored by Git. Do not paste API keys, access hashes, phone numbers, or raw private message text into issues or chats.
