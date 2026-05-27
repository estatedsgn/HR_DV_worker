# LangGraph Funnel Foundation

The active funnel foundation lives in `app/services/funnel_graph`.

## Runtime Ownership

- `lead_funnel_runtime` is the runtime source of truth for the new funnel.
- `lead_brain_state`, `lead_agenda_items`, Brain V2 gateway state, controlled runner phases, and campaign sequence runs are legacy funnel state and must not drive new funnel behavior.
- LangGraph checkpoints use `thread_id = dialog_id`, so a Telegram dialog resumes the same graph thread across inbound turns.

## Knowledge Base

- Existing `knowledge_cards` remain the answer database.
- New graph code reads them through `FunnelKnowledgeAdapter`, which wraps the existing `KnowledgeCardService`.
- Do not duplicate answers into JSON or a new answer table unless a later migration explicitly replaces `knowledge_cards`.

## Stage Handler Contract

Future funnel stages should be added as graph nodes or stage handlers that accept and return `FunnelGraphState`.

Stage handlers may update:

- `stage`
- `status`
- `current_goal`
- `slots`
- `metadata`
- `pending_actions`

Stage handlers must not send Telegram messages directly. They should append actions only. `FunnelActionExecutor` is the only component that converts actions into `Message`, `OutboundJob`, handoff, or close-lost changes.

## Action Format

- `send_text`: `text`, optional `delay_seconds`, optional `idempotency_key`
- `send_voice`: `media_path`, optional `caption`, optional `recording_delay_seconds`, optional `delay_seconds`, optional `idempotency_key`
- `handoff`: `reason`
- `close_lost`: `reason`

Use `idempotency_key` for every scripted send that can be retried or replayed.

## Legacy Components

Do not add new funnel logic to:

- `scripts/run_controlled_funnel.py`
- `app/services/brain_v2/*`
- `app/services/campaign_sequence.py`

Those modules can remain in the repo for historical compatibility, but new funnel behavior should be implemented under `funnel_graph`.
