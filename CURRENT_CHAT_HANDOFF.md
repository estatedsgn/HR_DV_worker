# Current Chat Handoff

Date: 2026-05-27  
Project: `HR_DV_worker`  
Current focus: terminal/live-testable LangGraph HR funnel for a candidate conversation.

## What Matters Now

The active work is the LangGraph funnel under:

- `app/services/funnel_graph/`
- terminal runner: `scripts/funnel_graph_local_chat.py`
- eval runner: `scripts/run_funnel_eval.py`
- static knowledge: `knowledge/`
- prompts: `prompts/`
- latest useful live logs: `runtime_logs/funnel_eval_alibaba_recheck_qwen_turbo_20260527/`

Older notes about `run_controlled_funnel.py`, `Brain V2` agenda, and live Telegram/CRMchat are stale for this phase. CRMchat is not the current focus. The user is testing the funnel in terminal/eval first.

## Current Architecture

The current graph flow is:

```text
load_state
-> semantic_analyzer
-> retrieve_knowledge
-> reply_orchestrator
-> state_controller
-> action_executor
-> save_state
```

Responsibilities:

- `semantic_analyzer`: first LLM/fallback pass. Understands meaning relative to the active question. Extracts facts and classifies message type.
- `retrieve_knowledge`: retrieves FAQ/objection/template knowledge from `knowledge/*.json` and optional DB knowledge cards.
- `reply_orchestrator`: second LLM/fallback pass. Writes a short answer only; it must not move stage.
- `state_controller`: only module allowed to move the funnel. Applies facts, checks required fields, blocks invalid transitions.
- `action_executor`: injects voice/template/action-stage outputs and auto-hops action stages.
- `save_state`: writes agent run fields used by eval reports.

This split was made because the user explicitly wanted:

- LLM understands semantic meaning, not dumb keyword matching;
- retrieval suggests allowed knowledge;
- LLM writes natural reply from knowledge;
- controller, not LLM, moves the funnel.

## Active Funnel Stages

Current main path:

```text
interest_check
-> age_check
-> work_intro_delivery
-> salary_schedule_offer
-> salary_schedule_delivery
-> post_equipment_questions_check
-> profile_theme_check
-> support_smalltalk
-> room_available_check
-> equipment_phone_check
-> interview_offer
-> contact_collection
-> interview_day_check
-> interview_time_check / interview_custom_time
-> human_handoff
```

Final/side stages:

```text
lost
do_not_contact
human_handoff
```

Legacy-compatible policies still exist for `company_intro`, `try_interest_check`, and `ready_for_interview`, but the currently verified path ends in `human_handoff` after collecting interview time.

Stage policies live in:

```text
app/services/funnel_graph/funnel_policy.py
```

Important current stage questions:

- `interest_check`: `Рассказать подробнее?`
- `age_check`: `Сколько тебе лет?`
- `salary_schedule_offer`: `Если интересна наша сфера, давай расскажу про зп и график`
- `post_equipment_questions_check`: `Остались ли у тебя какие-нибудь ещё вопросы?`
- `profile_theme_check`: `Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?`
- `room_available_check`: `Есть ли у тебя комната или место, где никто не будет мешать во время стримов?`
- `equipment_phone_check`: `Какая у тебя модель телефона?`
- `interview_offer`: `Можем записаться на собеседование?`
- `contact_collection`: `Для записи мне нужен твой номер телефона и имя`
- `interview_day_check`: `Завтра будет удобно провести собеседование?`
- `interview_time_check`: `С 11:00 по 18:00 в какое время будет удобнее?`

## Interrupt Algorithm

Current behavior after the last fixes:

1. If the candidate asks a question or raises an objection, it is an interrupt, not a new stage.
2. Bot answers using FAQ/objection knowledge.
3. Bot does not repeat the active funnel question in the same message.
4. Graph stores interrupt-followup metadata:
   - `awaiting_interrupt_followup=true`
   - `interrupt_followup_question`
   - `interrupt_followup_timeout_seconds=60`
5. Graph creates a delayed `send_text` action with `delay_seconds=60`.
6. If the candidate sends a new message before the delayed action sends, existing outbound cancellation logic cancels pending queued outbound jobs.
7. If no message arrives, the delayed follow-up returns to the active question naturally, e.g. `Что-то ещё осталось непонятным?`

Manual terminal simulation:

```powershell
.\.venv\Scripts\python.exe scripts\funnel_graph_local_chat.py --with-llm --reset
```

Inside terminal chat:

```text
/timeout
```

`/timeout` simulates the 60-second interrupt follow-up event.

Important fixes made:

- `latest_inbound_text()` ignores stale `recent_messages` when `timeout_event` is present.
- guard catches exact and paraphrased next-stage questions during interrupts.
- FAQ no longer contains hidden next-stage prompts like "therefore I ask phone model".
- `У меня есть стриминговое оборудование` on `equipment_phone_check` is treated as `partial_answer`, not interrupt. It does not close `phone_model`; bot asks softly:
  `О, круто! А чтобы мы точно всё настроили — какая у тебя модель телефона?`

## Knowledge And Prompts

Knowledge files:

- `knowledge/faq.json`
- `knowledge/objections.json`
- `knowledge/templates.json`
- `knowledge/voice_packs.json`

Important prompt files:

- `prompts/semantic_analyzer.md`
- `prompts/reply_orchestrator.md`
- `prompts/funnel_quality_evaluator.md`
- `prompts/dialogue_orchestrator.md` exists mostly for older/single-node compatibility.

Voice packs:

- `work_intro`
- `salary_schedule`

In terminal/eval they appear as:

```text
[voice_pack: work_intro]
[voice_pack: salary_schedule]
```

## Evaluator

The quality evaluator is implemented in:

```text
app/services/funnel_graph/evaluator.py
prompts/funnel_quality_evaluator.md
```

It evaluates completed eval runs using:

- transcript;
- turn logs;
- `state_before`;
- `semantic_result`;
- `retrieved_knowledge`;
- `reply_result`;
- `controller_decision`;
- `state_after`;
- `sent_messages`;
- `pending_actions`;
- error reports.

Criteria:

- `transition_safety`
- `interrupt_handling`
- `knowledge_grounding`
- `naturalness`
- `data_collection`
- `technical_health`

Important: the evaluator is an LLM-based judge, not ground truth. Always also inspect `turn_logs.jsonl` manually for critical turns.

## LLM Provider Status

`.env` is currently set for Alibaba Cloud Model Studio International through OpenAI-compatible endpoint.

Observed:

- `qwen-plus` failed on 2026-05-27 because Alibaba reported free tier exhausted:
  `The free tier of the model has been exhausted...`
- `qwen-turbo` worked through the same endpoint when overridden via environment variables.

Use this override for tests until `qwen-plus` is paid/enabled again:

```powershell
$env:LLM_MODEL='qwen-turbo'
$env:BRAIN_DIALOGUE_MODEL='qwen-turbo'
```

Do not print API keys.

## Last Verification

Unit tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_funnel_graph.py tests\test_funnel_eval_runner.py -q
```

Result:

```text
41 passed
```

Full live/eval run on Alibaba `qwen-turbo`:

```powershell
$env:LLM_MODEL='qwen-turbo'
$env:BRAIN_DIALOGUE_MODEL='qwen-turbo'
.\.venv\Scripts\python.exe scripts\run_funnel_eval.py --templates data\funnel_eval\templates.json --runs 1 --max-turns 25 --require-agent-llm --no-candidate-llm --with-quality-evaluator --require-quality-evaluator-llm --llm-timeout-seconds 45 --llm-max-retries 0 --output-dir runtime_logs\funnel_eval_alibaba_recheck_qwen_turbo_20260527
```

Result:

```text
runs=1 human_handoff=1 ready_for_interview=0 errors=0
quality_average_score=8.0
quality_passed=1
quality_high_or_critical_issues=0
```

Artifacts:

- `runtime_logs/funnel_eval_alibaba_recheck_qwen_turbo_20260527/transcripts.md`
- `runtime_logs/funnel_eval_alibaba_recheck_qwen_turbo_20260527/quality_report.md`
- `runtime_logs/funnel_eval_alibaba_recheck_qwen_turbo_20260527/turn_logs.jsonl`
- `runtime_logs/funnel_eval_alibaba_recheck_qwen_turbo_20260527/all_runs.json`

Previous stronger run on `qwen-plus` before free-tier exhaustion:

- `runtime_logs/funnel_eval_alibaba_interrupt_wait_final5/`
- score was `9.2`

## Key Verified Behaviors

From the latest `qwen-turbo` run:

- Candidate asks: `А можно вопрос, как ты нашла меня?`
  - stage stays `post_equipment_questions_check`;
  - answer uses `contact_source`;
  - delayed follow-up created with `delay_seconds=60`.

- Candidate asks: `А как оплата?`
  - stage stays `post_equipment_questions_check`;
  - answer uses `payment_process`;
  - delayed follow-up created.

- Candidate asks: `А если у меня нет компьютера или ноута, мне через телефон?`
  - stage stays `post_equipment_questions_check`;
  - answer: `Подойдёт современный телефон с нормальной камерой и стабильной связью.`
  - no immediate phone model question;
  - delayed follow-up created.

- Candidate says: `Пока вопросов нет`
  - `questions_resolved=true`;
  - stage moves to `profile_theme_check`.

- Candidate says: `У меня есть стриминговое оборудование`
  - stage stays `equipment_phone_check`;
  - semantic type is `partial_answer`;
  - `equipment_available=true`;
  - `phone_model` remains empty;
  - bot asks model softly.

- Candidate gives: `Samsung S25 Ultra`
  - `phone_model` saved;
  - stage moves to `interview_offer`.

- Contact partial:
  - number only -> bot asks name;
  - name after number -> stage moves to interview day.

- Final:
  - day/time collected;
  - stage becomes `human_handoff`;
  - final message normalizes tomorrow to `завтра`.

## Current Known Weak Spots

Not blockers:

- `qwen-turbo` is a bit more mechanical than `qwen-plus`.
- Latest quality evaluator flagged only low/medium wording comments, no high/critical issues.
- `quality_report.md` for latest run includes a contradictory "medium" issue that says no fix is needed. Treat that as evaluator noise.
- The final message can be made warmer later, e.g. `Записала тебя на завтра в 17:00, передам данные менеджеру.`
- Payment FAQ could include a soft non-stage follow-up like `Если что-то ещё по оплате непонятно, спрашивай`, but be careful not to reintroduce immediate active-question repetition.

## Files Most Relevant For Next Agent

- `app/services/funnel_graph/state.py`
- `app/services/funnel_graph/semantic.py`
- `app/services/funnel_graph/knowledge.py`
- `app/services/funnel_graph/reply.py`
- `app/services/funnel_graph/graph.py`
- `app/services/funnel_graph/funnel_policy.py`
- `app/services/funnel_graph/eval_runner.py`
- `app/services/funnel_graph/evaluator.py`
- `scripts/funnel_graph_local_chat.py`
- `scripts/run_funnel_eval.py`
- `tests/test_funnel_graph.py`
- `tests/test_funnel_eval_runner.py`
- `knowledge/faq.json`
- `knowledge/objections.json`
- `knowledge/templates.json`
- `knowledge/voice_packs.json`
- `prompts/semantic_analyzer.md`
- `prompts/reply_orchestrator.md`
- `prompts/funnel_quality_evaluator.md`

## Suggested Next Steps

1. Keep testing via terminal/eval before CRMchat.
2. If user wants manual chat:

```powershell
$env:LLM_MODEL='qwen-turbo'
$env:BRAIN_DIALOGUE_MODEL='qwen-turbo'
.\.venv\Scripts\python.exe scripts\funnel_graph_local_chat.py --with-llm --reset --candidate-id manual_1
```

3. If user wants another evaluation:

```powershell
$env:LLM_MODEL='qwen-turbo'
$env:BRAIN_DIALOGUE_MODEL='qwen-turbo'
.\.venv\Scripts\python.exe scripts\run_funnel_eval.py --templates data\funnel_eval\templates.json --runs 1 --max-turns 25 --require-agent-llm --no-candidate-llm --with-quality-evaluator --require-quality-evaluator-llm --llm-timeout-seconds 45 --llm-max-retries 0 --output-dir runtime_logs\funnel_eval_next
```

4. If `qwen-plus` is re-enabled in Alibaba console, remove the `qwen-turbo` env override and test again.

