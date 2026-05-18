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
