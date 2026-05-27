# Brain V2 Funnel And Algorithm

## Purpose

Brain V2 is the canonical dialogue brain for HR outreach conversations in CRMchat/Telegram.

It is implemented as an additive shadow-first layer beside the legacy campaign sequence pipeline. In shadow mode the brain builds and stores a decision, but does not send a CRMchat message until a human approves the saved `brain_run`.

## Canonical Funnel

`lead_brain_state.stage` is the source of truth for Brain V2.

Stages:

1. `lead_created`
2. `first_touch_sent`
3. `lead_replied`
4. `info_messages_sent`
5. `interest_triage`
6. `trust`
7. `age_gate`
8. `qualification`
9. `personalization`
10. `interview_close`
11. `contact_collection`
12. `scheduling`
13. `handoff`
14. `closed`

Legacy `Lead.funnel_state` is not the Brain V2 source of truth.

## High-Level Algorithm

1. CRMchat webhook or polling stores inbound Telegram messages.
2. `InboundQueueWorker` receives a non-outgoing inbound event.
3. If `BRAIN_V2_ENABLED=true`, `TurnBufferService` prepares the turn.
4. The turn buffer cancels pending outbound jobs when a newer inbound/typing event appears.
5. The turn buffer waits for `BRAIN_INBOUND_DEBOUNCE_SECONDS` after the latest candidate message or typing activity.
6. When the quiet window passes, `BrainGateway.decide_for_dialog_message()` runs.
7. Gateway loads or creates:
   - `Lead`
   - `LeadBrainState`
   - profile slots
   - agenda items
   - recent messages
8. Gateway builds `message_batch`: inbound candidate messages since the last actually sent outbound message.
9. Router/Extractor reads the combined latest candidate batch and returns:
   - primary intent
   - secondary intents
   - dialogue act
   - candidate questions
   - objections
   - slot patch
   - retrieval topics
   - confidence
10. If the lead is interested and has no agenda yet, State Manager creates the standard agenda.
11. Knowledge retrieval gets relevant `knowledge_cards` by:
   - trigger match
   - optional vector search
   - merge and rerank
12. Dialogue Brain chooses one dialogue move and one natural Russian Telegram response.
13. Validator checks the response.
14. State Manager applies:
    - slot patch
    - stage patch
    - open loop patch
    - agenda status updates
15. Gateway saves:
    - `brain_run`
    - `llm_calls`
    - `retrieval_events`
16. Executor handles the action:
    - in shadow mode: save only, no send
    - after approval: create normal `OutboundJob`

## Chaotic Multi-Message Turns

Telegram dialogs are not assumed to alternate one candidate message and one agent message.

Brain V2 uses a turn buffer:

- inbound events for older messages are skipped if a newer inbound already exists;
- a new inbound message cancels queued/retry outbound jobs for the same dialog;
- a candidate typing event also cancels queued/retry outbound jobs;
- the brain waits for a quiet window controlled by `BRAIN_INBOUND_DEBOUNCE_SECONDS`;
- after the wait, the brain decides from several recent messages plus `message_batch`;
- `brain_runs.message_batch` stores the inbound batch that was used for the decision;
- outbound worker checks again before send and after typing delay, so a late candidate message/typing event can still stop the send.

## Interest Triage

First touch and two information messages remain template/campaign-driven.

After a candidate replies:

- `interested`: create agenda and start Dialogue Brain.
- `not_interested`: close the lead.
- `unclear`: use classifier/router confidence and continue cautiously.

Current local fallback rules detect:

- hard refusal
- suspicious objection
- candidate question
- legal/risky question
- age
- phone/contact
- English/equipment/schedule signals
- interview/contact/scheduling signals

## Standard Agenda

Each item has:

- `item_key`
- `stage`
- `priority`
- `required`
- `completion_rule`
- `default_question`
- `slot_key`
- `next_stage_hint`

Items:

- `trust.answer_source`
- `trust.answer_basic_suspicion`
- `age.confirm_18`
- `qualification.check_english`
- `qualification.check_equipment`
- `qualification.check_availability`
- `qualification.check_boundaries`
- `personalization.collect_hobbies`
- `personalization.suggest_theme`
- `interview.offer`
- `contact.collect_name_phone`
- `schedule.collect_time`
- `handoff.prepare_summary`

Agenda item statuses:

- `pending`
- `active`
- `done`
- `blocked`
- `skipped`

## Dialogue Moves

Supported dialogue moves:

- `answer_and_reassure`
- `answer_and_resume_open_loop`
- `collect_missing_slot`
- `soft_qualify`
- `handle_trust_objection`
- `personalize_offer`
- `move_to_interview`
- `collect_contact`
- `schedule_interview`
- `handoff_to_human`
- `close_lost`
- `pause_and_follow_up`

The brain must send at most one short Telegram-style message per turn and ask at most one clear question.

## Open Loop

`open_loop` stores the current unresolved agent question.

When a candidate asks a side question, the brain should:

1. answer the candidate question;
2. resume the open loop if still relevant;
3. keep the response short.

Example:

- Open loop: “Скажи, пожалуйста, тебе уже есть 18?”
- Candidate: “А откуда ты меня нашла?”
- Brain move: `answer_and_resume_open_loop`
- Response shape: answer source briefly, then return to age question.

## Knowledge Cards

Brain V2 uses `knowledge_cards`, not legacy `knowledge_snippets`.

Fields include:

- `card_key`
- `stage`
- `topic`
- `content`
- `triggers`
- `tags`
- `verification_status`
- `active`
- `embedding`

Retrieval rules:

- only active cards;
- only approved or verified cards by default;
- stage-aware filtering;
- trigger/topic/tag match;
- optional vector similarity through pgvector;
- merge and rerank before sending cards to Dialogue Brain.

Seed file:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py
```

Profitcast bundle:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --profitcast-dir data\profitcast
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --profitcast-dir data\profitcast --embed
```

Nastya dialogue bundle:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\nastya
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\nastya --embed
```

Rina dialogue bundle:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\rina
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\rina --embed
```

Mentor Pavluck reference answers:

```powershell
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\mentor_pavluck
.\.venv\Scripts\python scripts\seed_knowledge_cards_v2.py --bundle-dir data\mentor_pavluck --embed
```

The Profitcast import reads:

- `data/profitcast/rag_seed_manifest_profitcast_v1.json`
- `data/profitcast/agenda_template_profitcast_v1.json`
- `data/profitcast/knowledge_cards_profitcast_v1.json`

The Nastya import reads:

- `data/nastya/rag_seed_manifest_nastya_v1.json`
- `data/nastya/knowledge_cards_nastya_v1.json`
- `data/nastya/agenda_additions_nastya_v1.json`
- `data/nastya/dialogue_chunks_nastya_v1.json`

The Rina import reads:

- `data/rina/knowledge_cards_rina_v1.json`
- `data/rina/agenda_additions_rina_v1.json`

The mentor Pavluck import reads:

- `data/mentor_pavluck/knowledge_cards_mentor_pavluck_v1.json`

The agenda registry merges bundled agenda items with the standard fallback agenda. Knowledge cards, mentor reference answers, and dialogue chunks are stored in `knowledge_cards`; embeddings are stored in the pgvector `embedding` column when `--embed` is used. Mentor reference cards get a small retrieval boost so the brain can prefer them as verified wording/fact anchors when topics overlap.

Profitcast-specific stages are normalized before use:

- `trust_source` -> `trust`
- `basic_info_pack`, `deep_info_pack`, `format_boundaries`, `qualification_faq`, `equipment_check`, `company_info` -> `qualification`
- `try_close` -> `interview_close`
- `first_touch` -> `first_touch_sent`
- `post_schedule_support` -> `scheduling`

## Executor Actions

Supported executor actions:

- `send_message`
- `schedule_followup`
- `handoff`
- `close_lost`
- `do_nothing`

With `BRAIN_SHADOW_MODE=true`, executor actions are saved in `brain_runs.executor_action` but not executed.

## Voice Intro

Four initial voice notes are stored under `data/voice_intro/` and described by `voice_intro_manifest.json`.

The current mapping is chronological because the files were exported without transcripts:

- `voice_intro_01_offer_overview.ogg` -> `first_touch_sent`, initial offer overview
- `voice_intro_02_work_format.ogg` -> `info_messages_sent`, work format overview
- `voice_intro_03_platform_process.ogg` -> `qualification`, platforms/process/support
- `voice_intro_04_next_steps.ogg` -> `interview_close`, next steps and interview

Queue them for a test dialog:

```powershell
.\.venv\Scripts\python.exe scripts\enqueue_voice_intro.py --username @iamnekiy --dry-run
.\.venv\Scripts\python.exe scripts\enqueue_voice_intro.py --username @iamnekiy --gap-seconds 4
```

Voice sending uses:

- `messages.setTyping` with `sendMessageRecordAudioAction`, so Telegram shows the recording indicator;
- `upload.saveFilePart` to upload OGG/Opus bytes;
- `messages.sendMedia` with `documentAttributeAudio.voice=true` to send a voice note.

Manual approval:

```powershell
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id> --show
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id>
```

Reject:

```powershell
.\.venv\Scripts\python scripts\approve_brain_run.py <brain-run-id> --reject --reason "bad tone"
```

API:

```http
GET /internal/brain-runs/{brain_run_id}
POST /internal/brain-runs/{brain_run_id}/approve
POST /internal/brain-runs/{brain_run_id}/reject
```

## Telemetry

Brain V2 stores:

- provider
- model
- prompt tokens
- completion tokens
- latency
- estimated cost
- validator verdict
- retrieved card ids
- stage before
- stage after
- dialogue move

Tables:

- `brain_runs`
- `llm_calls`
- `retrieval_events`

## Configuration

Core flags:

```env
BRAIN_V2_ENABLED=true
BRAIN_SHADOW_MODE=true
BRAIN_ENABLE_VALIDATOR=true
BRAIN_INBOUND_DEBOUNCE_SECONDS=10
BRAIN_CANCEL_OUTBOUND_ON_INBOUND=true
```

Provider keys:

```env
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Model settings:

```env
DEFAULT_EMBEDDING_PROVIDER=openai
DEFAULT_EMBEDDING_MODEL=text-embedding-3-small
BRAIN_DEFAULT_PROVIDER=openai
BRAIN_ROUTER_MODEL=gpt-4.1-mini
BRAIN_DIALOGUE_MODEL=gpt-4.1
BRAIN_VALIDATOR_MODEL=gpt-4.1-mini
```

## Current Safety Defaults

- Brain V2 is disabled unless `BRAIN_V2_ENABLED=true`.
- Shadow mode is enabled by default.
- Real sending still goes through existing outbound queue and allowlist guards.
- Legal/risky questions route to handoff.
- Underage candidates close the lead.
- Hard refusal closes the lead.
