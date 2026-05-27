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

## Terminal LangGraph HR Funnel

CRMchat can stay disabled while testing the managed funnel locally. The terminal runner uses the LangGraph flow:

```text
load_state -> semantic_analyzer -> retrieve_knowledge -> reply_orchestrator -> state_controller -> action_executor -> save_state
```

Start a fresh local candidate conversation:

```powershell
.\.venv\Scripts\python.exe scripts\funnel_graph_local_chat.py --candidate-id test_1 --reset
```

Run the scripted scenario non-interactively:

```powershell
.\.venv\Scripts\python.exe scripts\funnel_graph_local_chat.py --candidate-id test_1 --reset --script "Ну хорошо, расскажи" "18" "Хорошо, слушаю" "Пока вопросов нет" "Учусь, работаю, люблю рисовать" "Да, есть отдельная комната" "Samsung S25 Ultra" "Хорошо" "79999999999" "Диана" "Да" "примерно в 17.00"
```

The current production funnel order is:

```text
interest_check -> age_check -> work_intro_delivery -> salary_schedule_offer
-> salary_schedule_delivery -> post_equipment_questions_check
-> profile_theme_check -> support_smalltalk -> room_available_check
-> equipment_phone_check -> interview_offer -> contact_collection
-> interview_day_check -> interview_time_check / interview_custom_time
-> human_handoff
```

By default the terminal runner uses deterministic local semantic/reply fallbacks so it works without network access. To call the configured LLM adapter explicitly, add `--with-llm`.

Key files:

- Semantic prompt: `prompts/semantic_analyzer.md`
- Reply prompt: `prompts/reply_orchestrator.md`
- Stage policies: `app/services/funnel_graph/funnel_policy.py`
- Knowledge base: `knowledge/faq.json`, `knowledge/objections.json`, `knowledge/templates.json`
- Voice packs: `knowledge/voice_packs.json`
- Terminal state and agent runs: `runtime_logs/terminal_funnel/<candidate-id>.json` and `runtime_logs/terminal_funnel/agent_runs.jsonl`

Regression tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_funnel_graph.py -q
```

### Synthetic Funnel Eval

Use this runner to generate many candidate conversations against the live LangGraph funnel. The candidate simulator can use the LLM API, and the agent can use the LLM semantic/reply nodes.

Template example:

```text
data/funnel_eval/templates.example.json
```

Run 30 conversations with both LLM paths required. The command fails instead of silently falling back if either LLM path cannot run:

```powershell
.\.venv\Scripts\python.exe scripts\run_funnel_eval.py --templates data\funnel_eval\templates.example.json --runs 30 --max-turns 30 --require-agent-llm --require-candidate-llm --output-dir runtime_logs\funnel_eval_live
```

Run 24 conversations with a deterministic local candidate and the production LLM recruiter only:

```powershell
.\.venv\Scripts\python.exe scripts\run_funnel_eval.py --templates data\funnel_eval\templates.json --runs 24 --max-turns 30 --require-agent-llm --no-candidate-llm --llm-timeout-seconds 8 --llm-max-retries 0 --output-dir runtime_logs\funnel_eval_agent_llm_candidate_self
```

When `BRAIN_*` provider/model variables are not set, the Brain/LangGraph LLM adapter falls back to the generic `LLM_PROVIDER`, `LLM_MODEL`, and `LLM_BASE_URL`. A required-LLM eval run performs a fail-fast preflight and writes `api_error.json` / `api_error_report.md` if the API is unreachable.

Run deterministic smoke without LLM calls:

```powershell
.\.venv\Scripts\python.exe scripts\run_funnel_eval.py --templates data\funnel_eval\templates.example.json --runs 3 --no-agent-llm --no-candidate-llm --output-dir runtime_logs\funnel_eval_smoke
```

Outputs:

- `runtime_logs/funnel_eval_live/all_runs.json` - full structured transcripts, turn states, technical logs, and profile snapshots.
- `runtime_logs/funnel_eval_live/report.md` - readable report with complete conversations and per-turn technical JSON blocks.
- `runtime_logs/funnel_eval_live/transcripts.md` - conversations only, without technical state dumps.
- `runtime_logs/funnel_eval_live/turn_logs.jsonl` - one JSON log per turn: input, state before, semantic result, retrieved knowledge, reply result, controller decision, state after, sent messages, parse/controller errors.
- `runtime_logs/funnel_eval_live/state/` - per-candidate terminal state and `agent_runs.jsonl`.

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

## V1 test-production runbook

The V1 worker has a durable skeleton for lead intake, campaign sequencing, inbound/outbound queues, conservative sending, and a mini-LLM decision step.

Prepare the database and default campaign:

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python scripts\seed_default_campaign.py
```

Sync CRMChat Telegram accounts into the local account table:

```powershell
.\.venv\Scripts\python scripts\sync_crmchat_accounts.py --dry-run --report
.\.venv\Scripts\python scripts\sync_crmchat_accounts.py --report
```

Enqueue a controlled test lead:

```powershell
.\.venv\Scripts\python scripts\enqueue_lead.py --source smoke --external-lead-id smoke-1 --username '@iamnekiy'
```

Run workers once:

```powershell
.\.venv\Scripts\python scripts\poll_telegram_updates.py --all-accounts
.\.venv\Scripts\python scripts\process_inbound_queue.py --limit 100
.\.venv\Scripts\python scripts\process_outbound_queue.py --allow-real-send --limit 50
```

For a targeted live test where inbound messages should be marked as read, poll only the test username:

```powershell
.\.venv\Scripts\python scripts\poll_telegram_updates.py --all-accounts --only-username '@iamnekiy' --mark-read
```

Run workers continuously:

```powershell
.\.venv\Scripts\python scripts\poll_telegram_updates.py --all-accounts --loop
.\.venv\Scripts\python scripts\process_inbound_queue.py --loop --interval-seconds 10
.\.venv\Scripts\python scripts\process_outbound_queue.py --allow-real-send --loop --interval-seconds 10
```

Outbound safety defaults:

- `OUTBOUND_REAL_SEND_ENABLED=false` prevents accidental sends unless the worker is started with an explicit send flag.
- `OUTBOUND_ALLOWED_USERNAMES=@iamnekiy` restricts controlled real sends.
- `scripts/process_outbound_queue.py` exits without mutating jobs unless `--allow-real-send` is passed.
- For fast local smoke tests, set the selected account's `send_interval_seconds=10` and `send_jitter_seconds=0`; keep production intervals conservative.

Safe smoke checks:

```powershell
.\.venv\Scripts\python scripts\crmchat_smoke.py
.\.venv\Scripts\python scripts\crmchat_smoke.py --send-test
.\.venv\Scripts\python scripts\llm_decision_smoke.py
```

For local E2E without OpenAI calls, set `LLM_PROVIDER=mock`. The mock decision is read from `LLM_MOCK_DECISION_JSON`.

Recover stale sequence runs:

```powershell
.\.venv\Scripts\python scripts\recover_sequence_runs.py --older-than-seconds 900
```

## Brain V1 runbook

Brain V1 adds OpenRouter-compatible structured decisions, funnel states, lead facts, prompt versions, and a small CLI/API CRM surface.

Configure OpenRouter in `.env`:

```env
LLM_PROVIDER=openrouter
LLM_API_KEY=sk-or-change-me
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_ENDPOINT=chat_completions
LLM_MODEL=openai/gpt-5.4-mini
LLM_EMBEDDING_MODEL=openai/text-embedding-3-small
```

Seed the brain layer:

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python scripts\seed_brain_prompts.py
.\.venv\Scripts\python scripts\seed_brain_campaign.py
```

Enqueue a controlled brain test lead:

```powershell
.\.venv\Scripts\python scripts\enqueue_lead.py --source brain-smoke --external-lead-id brain-1 --username '@iamnekiy' --campaign-name brain-v1
```

Optional knowledge import uses JSONL records like `{"text":"...", "snippet_type":"objection", "tags":["pay"]}`:

```powershell
.\.venv\Scripts\python scripts\import_knowledge_snippets.py docs\samples\brain_snippets.local.jsonl
```

Mini-CRM CLI:

```powershell
.\.venv\Scripts\python scripts\show_lead_card.py --dialog-id <dialog-id>
.\.venv\Scripts\python scripts\update_lead_status.py --dialog-id <dialog-id> --funnel-state READY_FOR_HUMAN
```

The real Telegram send guard still applies: only allowlisted usernames can receive messages, and real sending still requires the explicit outbound worker flag.

Run one autonomous test loop for the allowlisted username:

```powershell
.\.venv\Scripts\python scripts\run_autopilot.py --all-accounts --only-username=@iamnekiy --allow-real-send --test-fast-pacing-seconds 1 --poll-interval-seconds 1 --recover-older-than-seconds 3 --typing-delay-seconds 5
```

By default the autopilot does not mark incoming messages read during polling. With `OUTBOUND_MARK_READ_ON_SEND=true`, it marks the latest inbound message read only after it sends the next reply. `--typing-delay-seconds 5` sends a Telegram typing action, waits 5 seconds, and then sends the reply. Use `--mark-read-on-poll` only for diagnostics.

For a long controlled brain test, keep the conversation open until at least 20 inbound tester messages before handoff:

```powershell
.\.venv\Scripts\python scripts\run_autopilot.py --all-accounts --only-username=@iamnekiy --allow-real-send --test-fast-pacing-seconds 1 --poll-interval-seconds 1 --recover-older-than-seconds 3 --typing-delay-seconds 2 --min-inbound-before-handoff 20
```

For production, remove `--only-username` only after the allowlist, account pacing, prompt quality, and monitoring are intentionally configured.

## Brain V2 shadow runbook

Brain V2 is an additive, shadow-first decision layer. When enabled with `BRAIN_SHADOW_MODE=true`, inbound messages create `brain_runs`, telemetry, state patches, and executor actions, but do not send through CRMchat until a run is approved.

Prepare the database and seed cards:

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py
```

Import the Profitcast JSON bundle as V2 knowledge cards:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --profitcast-dir data\profitcast
```

Import any RAG bundle that has `rag_seed_manifest_*.json`, including the Nastya dialogue bundle:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\nastya
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\rina
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\mentor_pavluck
```

Add `--embed` when provider keys are configured and the cards should be embedded into pgvector:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --profitcast-dir data\profitcast --embed
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\nastya --embed
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\rina --embed
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\mentor_pavluck --embed
```

Inbound turns are debounced before the brain replies. `BRAIN_INBOUND_DEBOUNCE_SECONDS=10` waits for a quiet window after the latest candidate message/typing activity. If another inbound message or typing event arrives before send, queued Brain V2 outbound jobs are cancelled and the next run is recomputed from the latest inbound batch.

Minimum shadow configuration:

```env
BRAIN_V2_ENABLED=true
BRAIN_SHADOW_MODE=true
BRAIN_ENABLE_VALIDATOR=true
BRAIN_INBOUND_DEBOUNCE_SECONDS=10
BRAIN_CANCEL_OUTBOUND_ON_INBOUND=true
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
BRAIN_DEFAULT_PROVIDER=openai
BRAIN_ROUTER_MODEL=gpt-4.1-mini
BRAIN_DIALOGUE_MODEL=gpt-4.1
BRAIN_VALIDATOR_MODEL=gpt-4.1-mini
```

Inspect and approve a shadow run:

```powershell
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id> --show
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id>
```

Reject a shadow run:

```powershell
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id> --reject --reason "bad tone"
```

HTTP equivalents:

```http
GET /internal/brain-runs/{brain_run_id}
POST /internal/brain-runs/{brain_run_id}/approve
POST /internal/brain-runs/{brain_run_id}/reject
```

## Voice intro runbook

The four initial voice notes are stored as OGG/Opus Telegram voice assets in `data/voice_intro/`.
The manifest maps them to the first funnel blocks:

- `voice_intro_01_offer_overview.ogg`: initial offer overview
- `voice_intro_02_work_format.ogg`: work format overview
- `voice_intro_03_platform_process.ogg`: platforms, process, support
- `voice_intro_04_next_steps.ogg`: next steps and interview close

Apply the voice-job migration:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Queue the intro voice notes for a known dialog or username:

```powershell
.\.venv\Scripts\python.exe scripts\enqueue_voice_intro.py --username @iamnekiy --dry-run
.\.venv\Scripts\python.exe scripts\enqueue_voice_intro.py --username @iamnekiy --gap-seconds 4
```

Voice outbound jobs use Telegram `sendMessageRecordAudioAction` before sending and then send the OGG file as a voice note via raw `upload.saveFilePart` + `messages.sendMedia`.
