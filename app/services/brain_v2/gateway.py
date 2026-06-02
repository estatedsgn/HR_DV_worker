from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.brain_v2 import BrainRun, LLMCall
from app.models.dialog import Dialog
from app.models.lead import Lead
from app.models.message import Message
from app.services.brain_v2.dialogue_brain import DialogueBrain
from app.services.brain_v2.executor import BrainExecutor
from app.services.brain_v2.knowledge_cards import KnowledgeCardService
from app.services.brain_v2.llm_provider import BrainLLMAdapter
from app.services.brain_v2.router import RouterExtractor
from app.services.brain_v2.schemas import (
    BrainGatewayDecision,
    BrainGatewayInput,
    BrainMessage,
    DialogueBrainDecision,
    ExecutorAction,
    RouterResult,
    StatePatch,
    ValidatorResult,
)
from app.services.brain_v2.state_manager import StateManager, agenda_item_to_dict, slots_to_dict
from app.services.brain_v2.turn_buffer import TurnBufferService
from app.services.brain_v2.validator import BrainValidator


class BrainGateway:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        llm_adapter: BrainLLMAdapter | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.llm_adapter = llm_adapter or BrainLLMAdapter(settings=self.settings)
        self.state_manager = StateManager(session)
        self.router = RouterExtractor()
        self.knowledge = KnowledgeCardService(session)
        self.dialogue_brain = DialogueBrain(self.llm_adapter)
        self.validator = BrainValidator(self.llm_adapter)
        self.executor = BrainExecutor(session, settings=self.settings)
        self.turn_buffer = TurnBufferService(
            session, debounce_seconds=self.settings.brain_inbound_debounce_seconds
        )

    async def decide_next_turn(self, input: BrainGatewayInput) -> BrainGatewayDecision:
        lead = await self.session.get(Lead, input.lead_id)
        dialog = await self.session.get(Dialog, input.conversation_id)
        if lead is None:
            raise ValueError(f"Lead not found: {input.lead_id}")
        if dialog is None:
            raise ValueError(f"Dialog not found: {input.conversation_id}")

        state = await self.state_manager.get_or_create_state(lead)
        if state.stage == "lead_created":
            await self.state_manager.update_stage(state, "lead_replied")
        state_snapshot = await self.state_manager.snapshot(lead)
        effective_incoming = combined_incoming_message(input.incoming_message, input.message_batch)
        router_result = await self.router.extract(
            incoming_message=effective_incoming,
            recent_messages=input.recent_messages,
            state_snapshot=state_snapshot,
        )
        if should_create_agenda(router_result, state_snapshot):
            await self.state_manager.create_agenda_for_interested_lead(lead)
            await self.state_manager.update_stage(state, "trust")
            state_snapshot = await self.state_manager.snapshot(lead)

        retrieval = await self.knowledge.retrieve_for_turn(
            query=effective_incoming.body,
            stage=str(state_snapshot.get("stage") or state.stage),
            topics=router_result.retrieval_topics,
            tags=[],
            top_k=5,
        )
        brain_decision = await self.dialogue_brain.decide(
            current_stage=str(state_snapshot.get("stage") or state.stage),
            current_goal=state_snapshot.get("current_goal"),
            open_loop=state_snapshot.get("open_loop"),
            profile_slots=dict(state_snapshot.get("profile_slots") or {}),
            agenda_items=list(state_snapshot.get("agenda_items") or []),
            recent_messages=input.recent_messages,
            last_lead_message=effective_incoming,
            router_result=router_result,
            retrieved_knowledge_cards=retrieval.cards,
        )
        validator_result = await self._validate_or_pass(
            current_stage=str(state_snapshot.get("stage") or state.stage),
            open_loop_before=state_snapshot.get("open_loop"),
            retrieved_cards=retrieval.cards,
            brain_decision=brain_decision,
        )
        if validator_result.verdict == "revise":
            brain_decision = await self.dialogue_brain.decide(
                current_stage=str(state_snapshot.get("stage") or state.stage),
                current_goal=state_snapshot.get("current_goal"),
                open_loop=state_snapshot.get("open_loop"),
                profile_slots=dict(state_snapshot.get("profile_slots") or {}),
                agenda_items=list(state_snapshot.get("agenda_items") or []),
                recent_messages=input.recent_messages,
                last_lead_message=effective_incoming,
                router_result=router_result,
                retrieved_knowledge_cards=retrieval.cards,
                revision_instruction=validator_result.revision_instruction,
            )
            validator_result = await self._validate_or_pass(
                current_stage=str(state_snapshot.get("stage") or state.stage),
                open_loop_before=state_snapshot.get("open_loop"),
                retrieved_cards=retrieval.cards,
                brain_decision=brain_decision,
            )
        if validator_result.verdict == "handoff":
            brain_decision = force_handoff(brain_decision, validator_result)

        slot_patch = {**router_result.slot_patch, **brain_decision.slot_patch}
        await self.state_manager.apply_slot_patch(
            lead,
            slot_patch,
            source="brain_v2",
            confidence=brain_decision.confidence,
        )
        await self.state_manager.apply_state_patch(state, brain_decision.state_patch)
        stage_after = state.stage

        brain_run = BrainRun(
            lead_id=lead.id,
            dialog_id=dialog.id,
            incoming_message_id=input.incoming_message.id,
            shadow_mode=self.settings.brain_shadow_mode,
            status="shadow_pending" if self.settings.brain_shadow_mode else "executed",
            stage_before=str(state_snapshot.get("stage") or state.stage),
            stage_after=stage_after,
            dialogue_move=brain_decision.dialogue_move,
            validator_verdict=validator_result.verdict,
            response_text=validator_result.approved_text or brain_decision.response,
            message_batch=[message.model_dump() for message in input.message_batch],
            router_result=router_result.model_dump(),
            retrieved_card_ids=[card.id for card in retrieval.cards],
            brain_decision=brain_decision.model_dump(),
            validator_result=validator_result.model_dump(),
            executor_action=brain_decision.executor_action.model_dump(),
            state_patch=brain_decision.state_patch.model_dump(),
        )
        self.session.add(brain_run)
        await self.session.flush()
        await self.knowledge.record_retrieval_event(
            brain_run_id=brain_run.id,
            lead_id=lead.id,
            dialog_id=dialog.id,
            stage=stage_after,
            query=effective_incoming.body,
            topics=router_result.retrieval_topics,
            result=retrieval,
        )
        await self._persist_llm_calls(brain_run)
        await self.executor.execute_or_shadow(
            brain_run=brain_run,
            dialog=dialog,
            action=brain_decision.executor_action,
            shadow_mode=self.settings.brain_shadow_mode,
        )
        await self.session.flush()
        await self.turn_buffer.mark_processed(lead, await self._message_by_id(input.incoming_message.id))
        return BrainGatewayDecision(
            decision_id=str(brain_run.id),
            router_result=router_result,
            retrieved_cards=retrieval.cards,
            brain_decision=brain_decision,
            validator_result=validator_result,
            executor_action=brain_decision.executor_action,
            state_patch=brain_decision.state_patch,
            brain_run_id=str(brain_run.id),
        )

    async def decide_for_dialog_message(self, *, dialog_id: str, message_id: str | None) -> BrainGatewayDecision | None:
        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            return None
        lead = await self._get_or_create_lead(dialog)
        message = await self.session.get(Message, message_id) if message_id else None
        if message is None:
            message = await self._latest_inbound(dialog.id)
        if message is None or message.direction == "outbound":
            return None
        recent_messages = await self._recent_messages(dialog.id)
        snapshot = await self.state_manager.snapshot(lead)
        batch = await self.turn_buffer.inbound_batch_since_last_outbound(dialog.id) or [message]
        input_payload = BrainGatewayInput(
            lead_id=str(lead.id),
            conversation_id=str(dialog.id),
            incoming_message=message_to_brain_message(message),
            recent_messages=[message_to_brain_message(item) for item in recent_messages],
            message_batch=[message_to_brain_message(item) for item in batch],
            current_state={k: v for k, v in snapshot.items() if k not in {"profile_slots", "agenda_items"}},
            profile_slots=dict(snapshot.get("profile_slots") or {}),
            agenda_items=[agenda_item_to_dict(item) for item in await self.state_manager.list_agenda(lead.id)],
            channel_meta={"telegram_username": dialog.telegram_username},
        )
        return await self.decide_next_turn(input_payload)

    async def _validate_or_pass(
        self,
        *,
        current_stage: str,
        open_loop_before: dict[str, Any] | None,
        retrieved_cards,
        brain_decision: DialogueBrainDecision,
    ) -> ValidatorResult:
        if not self.settings.brain_enable_validator:
            return ValidatorResult(verdict="pass", approved_text=brain_decision.response)
        return await self.validator.validate(
            current_stage=current_stage,
            open_loop_before=open_loop_before,
            retrieved_knowledge_cards=retrieved_cards,
            dialogue_decision=brain_decision,
            response_text=brain_decision.response,
            slot_patch=brain_decision.slot_patch,
            state_patch=brain_decision.state_patch.model_dump(),
        )

    async def _persist_llm_calls(self, brain_run: BrainRun) -> None:
        for telemetry in self.llm_adapter.telemetry:
            self.session.add(
                LLMCall(
                    brain_run_id=brain_run.id,
                    component=telemetry.component,
                    provider=telemetry.provider,
                    model=telemetry.model,
                    status=telemetry.status,
                    prompt_tokens=telemetry.prompt_tokens,
                    completion_tokens=telemetry.completion_tokens,
                    latency_ms=telemetry.latency_ms,
                    estimated_cost=telemetry.estimated_cost,
                    request_json=telemetry.request_json,
                    response_json=telemetry.response_json,
                    error_message=telemetry.error_message,
                )
            )
        self.llm_adapter.telemetry.clear()
        await self.session.flush()

    async def _get_or_create_lead(self, dialog: Dialog) -> Lead:
        result = await self.session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        lead = result.scalar_one_or_none()
        if lead is not None:
            return lead
        lead = Lead(dialog_id=dialog.id, qualification_status="new")
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def _latest_inbound(self, dialog_id) -> Message | None:
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id, Message.direction == "inbound")
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _recent_messages(self, dialog_id, *, limit: int = 20) -> list[Message]:
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        messages.reverse()
        return messages

    async def _message_by_id(self, message_id: str | None) -> Message | None:
        if not message_id:
            return None
        try:
            ident = UUID(str(message_id))
        except ValueError:
            return None
        return await self.session.get(Message, ident)


def should_create_agenda(router_result: RouterResult, state_snapshot: dict[str, Any]) -> bool:
    if state_snapshot.get("agenda_items"):
        return False
    if router_result.primary_intent in {"not_interested", "legal_risk"}:
        return False
    return router_result.primary_intent in {"interested", "question", "objection", "unclear"}


def force_handoff(decision: DialogueBrainDecision, validator: ValidatorResult) -> DialogueBrainDecision:
    text = validator.approved_text or decision.response or "Не хочу придумывать ответ наугад. Зафиксирую вопрос отдельно."
    return decision.model_copy(
        update={
            "response": text,
            "state_patch": StatePatch(stage="handoff", current_goal="Validator requested handoff"),
            "executor_action": ExecutorAction(type="handoff", text=text, handoff_reason=validator.handoff_reason or "validator_handoff"),
        }
    )


def message_to_brain_message(message: Message) -> BrainMessage:
    return BrainMessage(
        id=str(message.id),
        direction=message.direction,
        sender_type=message.sender_type,
        body=message.body,
        sent_at=message.sent_at.isoformat() if message.sent_at else None,
    )


def combined_incoming_message(incoming: BrainMessage, batch: list[BrainMessage]) -> BrainMessage:
    inbound_batch = [message for message in batch if message.direction == "inbound" and message.body.strip()]
    if len(inbound_batch) <= 1:
        return incoming
    combined_body = "\n".join(message.body.strip() for message in inbound_batch)
    return incoming.model_copy(update={"body": combined_body})
