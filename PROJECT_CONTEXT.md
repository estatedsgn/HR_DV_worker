# Project Context

## Product Goal

HR DV Worker automates Telegram recruiting conversations on behalf of the team.

The target production scale is approximately 50 Telegram accounts connected through CRMChat. These accounts should communicate with candidates, qualify them for work, collect required facts, and hand off suitable leads to a human operator.

## Current Stage

The project is currently testing the "brain" layer: the decision-making component that reads dialog history, applies deterministic rules where possible, calls the LLM when needed, extracts lead facts, advances funnel state, and decides whether to reply, ask a question, stop, or hand off to a human.

## Current Focus

- Stabilize the current LangGraph funnel before broad rollout.
- Keep live sending constrained to allowlisted usernames while testing.
- Verify prompt quality, funnel transitions, fact extraction, and handoff readiness.
- Make sure account pacing, outbound safety, and monitoring are ready before expanding beyond controlled tests.
- Current live-test goal: make the agent hold an autonomous controlled dialog with the tester for at least 20 inbound messages before handing off.
- Legacy Brain V2/RAG seed bundles were removed from the active tree. Current funnel knowledge lives in `knowledge/*.json`; new dialogue examples should be generated from transcripts through the semantic layer before being added.
- Four initial Telegram voice notes are stored under `data/voice_intro/`; they are queued by `scripts/enqueue_voice_intro.py` and sent with `sendMessageRecordAudioAction` before raw media upload/send.
- Candidate turns are buffered: the brain waits for a quiet window after the latest inbound or typing event, then decides from the recent history plus a batch of inbound messages since the last actually sent agent message.

## Important Constraints

- The system will eventually operate across about 50 Telegram accounts.
- The bot communicates as the team, so tone, safety, and lead qualification accuracy matter.
- Real Telegram sending must remain guarded during testing.
- CRMChat is the current integration boundary for Telegram account access.
- Long brain tests can use `BRAIN_MIN_INBOUND_BEFORE_HANDOFF` or the autopilot flag `--min-inbound-before-handoff` to prevent early handoff while testing.
- Brain V2 can be enabled with `BRAIN_V2_ENABLED=true`; with `BRAIN_SHADOW_MODE=true`, decisions are saved to `brain_runs` and require manual approval before creating outbound jobs.
- `BRAIN_INBOUND_DEBOUNCE_SECONDS` controls the quiet-window timer; `BRAIN_CANCEL_OUTBOUND_ON_INBOUND=true` cancels pending outbound replies when the candidate sends or starts typing before our answer.

## Working Notes

- When new durable project facts appear, add them here.
- Use README for operator runbooks and commands.
- Use tests for executable behavior guarantees.
- Keep temporary debugging files out of committed project state unless they become intentional tooling.
