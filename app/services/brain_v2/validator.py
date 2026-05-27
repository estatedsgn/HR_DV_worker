from __future__ import annotations

from typing import Any

from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.brain_v2.schemas import DialogueBrainDecision, RetrievedKnowledgeCard, ValidatorResult


class BrainValidator:
    def __init__(self, llm_adapter: BrainLLMAdapter | None = None) -> None:
        self.llm_adapter = llm_adapter or BrainLLMAdapter()

    async def validate(
        self,
        *,
        current_stage: str,
        open_loop_before: dict[str, Any] | None,
        retrieved_knowledge_cards: list[RetrievedKnowledgeCard],
        dialogue_decision: DialogueBrainDecision,
        response_text: str | None,
        slot_patch: dict[str, Any],
        state_patch: dict[str, Any],
    ) -> ValidatorResult:
        local = local_validate(
            current_stage=current_stage,
            open_loop_before=open_loop_before,
            dialogue_decision=dialogue_decision,
            response_text=response_text,
        )
        if local.verdict != "pass" or not self.llm_adapter.has_api_key("validator"):
            return local
        try:
            payload = await self.llm_adapter.complete_json(
                component="validator",
                system_prompt=VALIDATOR_PROMPT,
                user_payload={
                    "current_stage": current_stage,
                    "open_loop_before": open_loop_before,
                    "retrieved_knowledge_cards": [card.model_dump() for card in retrieved_knowledge_cards],
                    "dialogue_decision": dialogue_decision.model_dump(),
                    "response_text": response_text,
                    "slot_patch": slot_patch,
                    "state_patch": state_patch,
                },
                response_model=ValidatorResult,
            )
            return ValidatorResult.model_validate(payload)
        except BrainLLMError:
            return local


VALIDATOR_PROMPT = """Validate an HR Telegram reply.
Return JSON only. Ensure one short Russian message, no unsafe claims, no AI/internal wording,
and no contradiction with provided knowledge cards."""


def local_validate(
    *,
    current_stage: str,
    open_loop_before: dict[str, Any] | None,
    dialogue_decision: DialogueBrainDecision,
    response_text: str | None,
) -> ValidatorResult:
    issues: list[str] = []
    if dialogue_decision.executor_action.type == "send_message" and not response_text:
        issues.append("send_message action has no response text")
    if response_text and len(response_text) > 700:
        issues.append("response is too long for one Telegram message")
    lowered = (response_text or "").lower()
    if any(marker in lowered for marker in ["prompt", "schema", "искусственный интеллект", "я ии"]):
        issues.append("response mentions internal/AI wording")
    if dialogue_decision.executor_action.type == "handoff":
        return ValidatorResult(
            verdict="handoff",
            quality_score=0.8,
            issues=issues,
            approved_text=response_text,
            handoff_reason=dialogue_decision.executor_action.handoff_reason,
        )
    if issues:
        return ValidatorResult(
            verdict="revise",
            quality_score=0.45,
            issues=issues,
            revision_instruction="Return one short natural Russian Telegram message without internal wording.",
        )
    return ValidatorResult(
        verdict="pass",
        quality_score=0.95,
        issues=[],
        approved_text=response_text,
    )
