# Current LangGraph Funnel Prompts

This project currently uses one active LangGraph funnel pipeline:

```text
load_state
-> semantic_analyzer
-> retrieve_knowledge
-> reply_orchestrator
-> state_controller
-> action_executor
-> save_state
```

## Active Prompt Files

- `prompts/semantic_analyzer.md`
  - Used by `app/services/funnel_graph/semantic.py`.
  - Classifies the candidate message, extracts facts, detects interrupts, and selects retrieval topics.
  - Does not write the final reply and does not move the stage.

- `prompts/reply_orchestrator.md`
  - Used by `app/services/funnel_graph/reply.py`.
  - Final reply generator. Receives `message_batch`, dialogue context, semantic result, and retrieved FAQ/knowledge options.
  - Does not move the stage; `state_controller` owns transitions.

- `prompts/funnel_quality_evaluator.md`
  - Used by `app/services/funnel_graph/evaluator.py`.
  - Evaluates completed terminal/eval runs from transcripts, turn logs, semantic results, controller decisions, and errors.

## Archived Legacy Prompts

Legacy prompt and pipeline files were moved to:

```text
archive/legacy_funnel_20260528/
```

Do not edit archived prompts for the current funnel. If a behavior change is needed, update one of the active prompt files above or the current LangGraph code under `app/services/funnel_graph/`.
