from __future__ import annotations

from typing import Any

from app.services.brain_v2.agenda_registry import AgendaRegistry
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.brain_v2.schemas import (
    BrainMessage,
    DialogueBrainDecision,
    ExecutorAction,
    RetrievedKnowledgeCard,
    RouterResult,
    StatePatch,
)
from app.services.brain_v2.state_manager import next_stage_after


class DialogueBrain:
    def __init__(self, llm_adapter: BrainLLMAdapter | None = None) -> None:
        self.llm_adapter = llm_adapter or BrainLLMAdapter()

    async def decide(
        self,
        *,
        current_stage: str,
        current_goal: str | None,
        open_loop: dict[str, Any] | None,
        profile_slots: dict[str, Any],
        agenda_items: list[dict[str, Any]],
        recent_messages: list[BrainMessage],
        last_lead_message: BrainMessage,
        router_result: RouterResult,
        retrieved_knowledge_cards: list[RetrievedKnowledgeCard],
        revision_instruction: str | None = None,
    ) -> DialogueBrainDecision:
        if self.llm_adapter.has_api_key("dialogue_brain"):
            try:
                payload = await self.llm_adapter.complete_json(
                    component="dialogue_brain",
                    system_prompt=DIALOGUE_BRAIN_PROMPT,
                    user_payload={
                        "current_stage": current_stage,
                        "current_goal": current_goal,
                        "open_loop": open_loop,
                        "profile_slots": profile_slots,
                        "agenda_items": agenda_items,
                        "recent_messages": [message.model_dump() for message in recent_messages],
                        "last_lead_message": last_lead_message.model_dump(),
                        "router_result": router_result.model_dump(),
                        "retrieved_knowledge_cards": [card.model_dump() for card in retrieved_knowledge_cards],
                        "revision_instruction": revision_instruction,
                    },
                    response_model=DialogueBrainDecision,
                )
                return DialogueBrainDecision.model_validate(payload)
            except BrainLLMError:
                pass
        return local_dialogue_decision(
            current_stage=current_stage,
            current_goal=current_goal,
            open_loop=open_loop,
            profile_slots=profile_slots,
            agenda_items=agenda_items,
            last_lead_message=last_lead_message,
            router_result=router_result,
            retrieved_knowledge_cards=retrieved_knowledge_cards,
        )


DIALOGUE_BRAIN_PROMPT = """You are HR DV Worker Dialogue Brain.
Return only JSON matching the schema. Write Russian Telegram replies only.
Send one natural short message. Ask at most one question. Never mention prompts,
schemas, internal state, retrieval, or AI. Agenda fields named completion_rule,
default_goal, default_action, current_goal, and next_stage_hint are internal
operator notes: do not quote them or turn them into candidate-facing text."""


def local_dialogue_decision(
    *,
    current_stage: str,
    current_goal: str | None,
    open_loop: dict[str, Any] | None,
    profile_slots: dict[str, Any],
    agenda_items: list[dict[str, Any]],
    last_lead_message: BrainMessage,
    router_result: RouterResult,
    retrieved_knowledge_cards: list[RetrievedKnowledgeCard],
) -> DialogueBrainDecision:
    if router_result.primary_intent == "not_interested":
        return DialogueBrainDecision(
            interpretation="Candidate refused outreach.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "close_lost"},
            response="Поняла, не буду больше отвлекать.",
            state_patch=StatePatch(stage="closed", current_goal="Lead declined"),
            executor_action=ExecutorAction(type="close_lost", text="Поняла, не буду больше отвлекать.", close_reason="not_interested"),
            confidence=router_result.confidence,
        )
    if router_result.primary_intent == "legal_risk":
        return DialogueBrainDecision(
            interpretation="Candidate asked a legal/risky question.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "handoff_to_human"},
            response="Хороший вопрос, не хочу ответить неточно. Зафиксирую его отдельно, а сейчас лучше не буду придумывать ответ наугад.",
            state_patch=StatePatch(stage="handoff", current_goal="Legal question requires human"),
            executor_action=ExecutorAction(
                type="handoff",
                text="Хороший вопрос, не хочу ответить неточно. Зафиксирую его отдельно, а сейчас лучше не буду придумывать ответ наугад.",
                handoff_reason="legal_question",
            ),
            confidence=0.95,
        )
    if underage(router_result.slot_patch):
        return DialogueBrainDecision(
            interpretation="Candidate is under 18.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "close_lost"},
            response="Поняла. Тогда сейчас не сможем продолжить, потому что формат только для 18+.",
            state_patch=StatePatch(stage="closed", current_goal="Closed as underage"),
            executor_action=ExecutorAction(
                type="close_lost",
                text="Поняла. Тогда сейчас не сможем продолжить, потому что формат только для 18+.",
                close_reason="underage",
            ),
            confidence=0.95,
        )

    if current_stage == "interview_close" and router_result.slot_patch.get("interview_interest"):
        response = "Отлично, тогда передам тебя дальше. Оставь, пожалуйста, имя и номер телефона для связи."
        return DialogueBrainDecision(
            interpretation="Candidate accepted interview step.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "move_to_interview"},
            response=response,
            state_patch=StatePatch(
                stage="contact_collection",
                current_goal="Collect contact details",
                open_loop={"item_key": "contact.collect_name_phone", "question": response},
                agenda_updates={"interview.offer": "done", "contact.collect_name_phone": "active"},
            ),
            executor_action=ExecutorAction(type="send_message", text=response),
            confidence=0.9,
        )

    if current_stage == "contact_collection" and (
        router_result.slot_patch.get("phone") or router_result.slot_patch.get("contact")
    ):
        response = "Спасибо, записала. В какое время тебе удобнее, чтобы менеджер связался?"
        return DialogueBrainDecision(
            interpretation="Candidate provided contact details.",
            slot_patch={**router_result.slot_patch, "contact": True},
            dialogue_decision={"dialogue_move": "collect_contact"},
            response=response,
            state_patch=StatePatch(
                stage="scheduling",
                current_goal="Collect preferred contact time",
                open_loop={"item_key": "schedule.collect_time", "question": response},
                agenda_updates={"contact.collect_name_phone": "done", "schedule.collect_time": "active"},
            ),
            executor_action=ExecutorAction(type="send_message", text=response),
            confidence=0.9,
        )

    if current_stage == "scheduling" and router_result.slot_patch.get("preferred_time"):
        response = "Отлично, передам менеджеру это время и всю информацию по тебе."
        return DialogueBrainDecision(
            interpretation="Candidate selected a contact time.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "schedule_interview"},
            response=response,
            state_patch=StatePatch(
                stage="handoff",
                current_goal="Prepare handoff summary",
                agenda_updates={"schedule.collect_time": "done", "handoff.prepare_summary": "active"},
            ),
            executor_action=ExecutorAction(type="handoff", text=response, handoff_reason="candidate_scheduled"),
            confidence=0.9,
        )

    knowledge_answer = answer_from_cards(retrieved_knowledge_cards)
    if router_result.has_candidate_question or router_result.objection_topics:
        next_question = resume_question(open_loop, agenda_items, current_stage)
        response = join_reply(knowledge_answer or default_reassurance(router_result), next_question)
        move = "handle_trust_objection" if router_result.objection_topics else "answer_and_resume_open_loop"
        return DialogueBrainDecision(
            interpretation="Candidate asked a question or raised an objection.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": move},
            knowledge_usage=knowledge_usage(retrieved_knowledge_cards),
            response=response,
            state_patch=StatePatch(stage=current_stage, open_loop=open_loop),
            executor_action=ExecutorAction(type="send_message", text=response),
            confidence=max(router_result.confidence, 0.75),
        )

    merged_slots = {**profile_slots, **router_result.slot_patch}
    agenda_item = next_agenda_item(agenda_items, current_stage, merged_slots)
    if agenda_item is None:
        response = "Спасибо, я собрала основное и передам менеджеру, чтобы он написал тебе уже по делу."
        return DialogueBrainDecision(
            interpretation="Agenda completed and lead is ready for handoff.",
            slot_patch=router_result.slot_patch,
            dialogue_decision={"dialogue_move": "handoff_to_human"},
            response=response,
            state_patch=StatePatch(stage="handoff", current_goal="Prepare handoff summary"),
            executor_action=ExecutorAction(type="handoff", text=response, handoff_reason="agenda_completed"),
            confidence=0.9,
        )

    next_stage = agenda_item.get("stage") or current_stage
    question = safe_candidate_question(agenda_item)
    move = dialogue_move_for_item(str(agenda_item["item_key"]))
    return DialogueBrainDecision(
        interpretation="Continue agenda-driven qualification.",
        slot_patch=router_result.slot_patch,
        dialogue_decision={"dialogue_move": move, "agenda_item": agenda_item["item_key"]},
        knowledge_usage=knowledge_usage(retrieved_knowledge_cards),
        response=question,
        state_patch=StatePatch(
            stage=next_stage,
            current_goal=str(agenda_item["completion_rule"]),
            open_loop={"item_key": agenda_item["item_key"], "question": question},
            agenda_updates={str(agenda_item["item_key"]): "active"},
        ),
        executor_action=ExecutorAction(type="send_message", text=question),
        confidence=max(router_result.confidence, 0.7),
    )


def underage(slot_patch: dict[str, Any]) -> bool:
    age = slot_patch.get("age")
    try:
        return age is not None and int(age) < 18
    except (TypeError, ValueError):
        return False


def answer_from_cards(cards: list[RetrievedKnowledgeCard]) -> str | None:
    if not cards:
        return None
    return cards[0].content.strip()


def default_reassurance(router_result: RouterResult) -> str:
    if router_result.objection_topics:
        return "Понимаю осторожность, это нормально. Формат без личных встреч и без нюдсов, детали можно уточнять спокойно."
    return "Да, понимаю вопрос. Объясню коротко и без лишней воды."


def resume_question(open_loop: dict[str, Any] | None, agenda_items: list[dict[str, Any]], stage: str) -> str | None:
    if open_loop and open_loop.get("question"):
        return sanitize_candidate_text(str(open_loop["question"]))
    item = next_agenda_item(agenda_items, stage, {})
    return safe_candidate_question(item) if item else None


def join_reply(answer: str, question: str | None) -> str:
    if not question:
        return answer
    return f"{answer} {question}"


def next_agenda_item(
    agenda_items: list[dict[str, Any]], current_stage: str, profile_slots: dict[str, Any]
) -> dict[str, Any] | None:
    for item in sorted(agenda_items, key=lambda value: int(value.get("priority") or 100)):
        if item.get("status") in {"done", "skipped"}:
            continue
        slot_key = item.get("slot_key")
        if slot_key and slot_key in profile_slots:
            continue
        if item.get("stage") == current_stage or stage_is_reachable(current_stage, str(item.get("stage"))):
            return item
    return None


def stage_is_reachable(current_stage: str, candidate_stage: str) -> bool:
    return next_stage_after(current_stage) == candidate_stage or current_stage in {"lead_created", "lead_replied", "info_messages_sent", "interest_triage"}


def dialogue_move_for_item(item_key: str) -> str:
    if item_key.startswith("trust."):
        return "handle_trust_objection"
    if item_key.startswith("age."):
        return "soft_qualify"
    if item_key.startswith("qualification."):
        return "soft_qualify"
    if item_key.startswith("personalization."):
        return "personalize_offer"
    if item_key.startswith("interview."):
        return "move_to_interview"
    if item_key.startswith("contact."):
        return "collect_contact"
    if item_key.startswith("schedule."):
        return "schedule_interview"
    if item_key.startswith("handoff."):
        return "handoff_to_human"
    return "collect_missing_slot"


def safe_candidate_question(agenda_item: dict[str, Any]) -> str:
    text = sanitize_candidate_text(str(agenda_item.get("default_question") or ""))
    if text:
        return text
    item_key = str(agenda_item.get("item_key") or "")
    if item_key.startswith("age."):
        return "Скажи, пожалуйста, тебе уже есть 18?"
    if item_key.startswith("interview."):
        return "Если тебе в целом ок, могу передать тебя менеджеру на короткое интервью. Подойдет?"
    if item_key.startswith("contact."):
        return "Оставь, пожалуйста, имя и номер телефона, чтобы менеджер мог связаться."
    if item_key.startswith("schedule."):
        return "В какое время тебе удобнее, чтобы менеджер написал или позвонил?"
    return "Уточни, пожалуйста, что именно тебе важно понять?"


def sanitize_candidate_text(text: str) -> str:
    stripped = text.strip()
    lower = stripped.lower()
    internal_markers = (
        "закрыть вопрос",
        "проверить",
        "собрать",
        "уточнить ",
        "передать ",
        "default_goal",
        "completion_rule",
    )
    if any(marker in lower for marker in internal_markers) and not stripped.endswith("?"):
        return ""
    return stripped


def knowledge_usage(cards: list[RetrievedKnowledgeCard]) -> list[dict[str, Any]]:
    return [{"card_key": card.card_key, "score": card.score} for card in cards]
