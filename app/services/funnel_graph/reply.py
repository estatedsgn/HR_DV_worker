from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.funnel_policy import STAGE_POLICIES, stage_requirement_met
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.semantic import SemanticResult, is_recoverable_llm_format_error, repair_mojibake
from app.services.funnel_graph.state import FunnelGraphState


PROMPT_PATH = PROJECT_ROOT / "prompts" / "reply_orchestrator.md"


class ReplyOutgoingMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "text"
    text: str | None = None
    voice_pack_id: str | None = None
    template_id: str | None = None


class ReplyResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    send_reply: bool = True
    outgoing_messages: list[ReplyOutgoingMessage] = Field(default_factory=list)
    reply_text: str | None = None
    handoff_required: bool = False
    handoff_reason: str | None = None
    summary: str = ""
    confidence: float = 0.0


class ReplyOrchestrator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
        prompt_path: Path | None = None,
        use_llm: bool = True,
        fallback_on_llm_error: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter = adapter or BrainLLMAdapter(settings=self.settings)
        self.prompt_path = prompt_path or PROMPT_PATH
        self.use_llm = use_llm
        self.fallback_on_llm_error = fallback_on_llm_error

    async def run(self, state: FunnelGraphState) -> ReplyResult:
        semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
        if semantic.message_type == "empty":
            return deterministic_reply(state)
        if self.use_llm and self.adapter.has_api_key("dialogue_brain"):
            try:
                payload = await self.adapter.complete_json(
                    component="dialogue_brain",
                    system_prompt=self.prompt_path.read_text(encoding="utf-8"),
                    user_payload={
                        "candidate_state": state.get("candidate_profile") or {},
                        "current_state": state.get("stage"),
                        "current_goal": state.get("current_goal"),
                        "pending_question": state.get("pending_question_text") or state.get("current_question"),
                        "incoming_message": state.get("incoming_message"),
                        "semantic_result": state.get("semantic_result") or {},
                        "faq_context": state.get("faq_context") or [],
                        "objection_context": state.get("objection_context") or [],
                        "retrieved_knowledge": state.get("retrieved_knowledge") or {},
                        "response_rules": state.get("response_rules") or {},
                    },
                    response_model=ReplyResult,
                )
                parsed = ReplyResult.model_validate(payload)
                guarded = guard_reply_with_policy(state, parsed)
                if guarded is not None:
                    return guarded
                if parsed.outgoing_messages or parsed.send_reply is False:
                    return parsed
            except BrainLLMError as exc:
                if not (self.fallback_on_llm_error or is_recoverable_llm_format_error(exc)):
                    raise
            except (ValidationError, ValueError, TypeError):
                pass
            except Exception:
                return deterministic_reply(state)
        return deterministic_reply(state)


def guard_reply_with_policy(state: FunnelGraphState, parsed: ReplyResult) -> ReplyResult | None:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    if semantic.message_type == "empty":
        return None
    if semantic.current_goal_satisfied and not semantic.has_unresolved_interrupt:
        return ReplyResult(
            send_reply=True,
            outgoing_messages=[],
            reply_text=None,
            summary="controller will continue",
            confidence=max(parsed.confidence or 0.0, 0.85),
        )
    if semantic.message_type == "partial_answer":
        stage = str(state.get("stage") or "interest_check")
        merged_profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
        if not stage_requirement_met(stage, merged_profile):
            followup = partial_followup(stage, state, semantic)
            if followup:
                return text_reply(
                    followup,
                    "policy partial followup",
                    handoff_required=parsed.handoff_required,
                    handoff_reason=parsed.handoff_reason,
                )
    if not semantic.has_unresolved_interrupt:
        merged_profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
        if stage_requirement_met(str(state.get("stage") or "interest_check"), merged_profile):
            return ReplyResult(
                send_reply=True,
                outgoing_messages=[],
                reply_text=None,
                summary="controller will continue after completed fields",
                confidence=max(parsed.confidence or 0.0, 0.85),
            )
    if not semantic.has_unresolved_interrupt:
        return None

    stage = str(state.get("stage") or "interest_check")
    current_question = str(state.get("pending_question_text") or state.get("current_question") or "")
    outgoing_text = "\n\n".join(str(message.text or "") for message in parsed.outgoing_messages if message.type == "text")

    if semantic.interrupt_type == "unclear":
        return deterministic_reply(state)
    if current_question and current_question in outgoing_text:
        return deterministic_reply(state)
    if contains_other_stage_question(outgoing_text, current_question, stage):
        return deterministic_reply(state)
    return None


def contains_other_stage_question(text: str, current_question: str, current_stage: str) -> bool:
    if not text:
        return False
    normalized = normalize_reply_text(text)
    for stage, policy in STAGE_POLICIES.items():
        question = policy.current_question
        if not question or stage == current_stage or question == current_question:
            continue
        if question in text:
            return True
        if stage_question_like(stage, normalized):
            return True
    return False


def normalize_reply_text(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def stage_question_like(stage: str, text: str) -> bool:
    if stage == "age_check":
        return "сколько тебе лет" in text or "возраст" in text
    if stage == "salary_schedule_offer":
        return ("зарплат" in text or "зп" in text) and "график" in text and any(marker in text for marker in ("рассказать", "интерес", "хочешь"))
    if stage == "post_equipment_questions_check":
        return "остал" in text and "вопрос" in text
    if stage == "profile_theme_check":
        return "расскажи" in text and "себ" in text and ("учишь" in text or "работ" in text or "свобод" in text)
    if stage == "room_available_check":
        return ("комнат" in text or "место" in text) and ("меш" in text or "никто" in text)
    if stage == "equipment_phone_check":
        return (
            "модель телефон" in text
            or "моделька телефон" in text
            or ("какой" in text and "телефон" in text)
            or "какая у тебя модель" in text
            or ("модель" in text and "телефон" in text and any(marker in text for marker in ("уточн", "скажи", "подскажи", "важн")))
        )
    if stage == "interview_offer":
        return "запис" in text and "собесед" in text
    if stage == "contact_collection":
        return "номер" in text and "им" in text
    if stage == "interview_day_check":
        return "завтра" in text and "собесед" in text and any(marker in text for marker in ("удоб", "можем", "получ"))
    if stage == "interview_time_check":
        return ("11:00" in text or "18:00" in text or "11 до 18" in text) and "время" in text
    if stage == "interview_custom_time":
        return "когда" in text and "удоб" in text and "собесед" in text
    return False


def deterministic_reply(state: FunnelGraphState) -> ReplyResult:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    stage = str(state.get("stage") or "interest_check")
    current_question = str(state.get("pending_question_text") or state.get("current_question") or "")
    templates = dict((state.get("metadata") or {}).get("templates") or {})

    if semantic.message_type == "empty":
        timeout_reply = interrupt_timeout_reply(state)
        if timeout_reply is not None:
            return timeout_reply
        if state.get("timeout_event"):
            return ReplyResult(send_reply=False, summary="empty timeout event", confidence=1.0)
        first_touch = str((state.get("retrieved_knowledge") or {}).get("first_touch_message") or templates.get("first_touch_message") or "")
        return text_reply(first_touch or current_question, "first touch")
    if semantic.message_type == "do_not_contact":
        return ReplyResult(send_reply=False, summary="do not contact", confidence=1.0)
    if semantic.message_type == "hard_refusal":
        return text_reply(str(templates.get("lost_message") or "Поняла, не буду отвлекать. Хорошего дня!"), "hard refusal")
    if semantic.message_type == "pause":
        return text_reply("Хорошо, буду ждать.", "pause")

    answer = knowledge_answer(state)
    if semantic.has_unresolved_interrupt:
        if semantic.interrupt_type == "unclear":
            incoming = repair_mojibake(str(state.get("incoming_message") or "")).strip().lower()
            if stage == "interest_check" and incoming in {"привет", "приветик", "здравствуйте", "добрый день"}:
                return text_reply(join_text("Привет!", current_question), "greeting at first reply")
            if stage == "post_equipment_questions_check":
                return text_reply(
                    "Поняла. Тогда уточню: остались ли у тебя ещё вопросы по условиям, оплате или формату?",
                    "natural questions followup",
                )
            return text_reply(join_text("Не совсем поняла, уточни, пожалуйста.", current_question), "unclear")
        if not answer:
            answer = "По этому вопросу лучше уточнить у менеджера, чтобы не сказать неточно."
            important = stage in {"contact_collection", "interview_day_check", "interview_time_check", "interview_custom_time"}
            if important:
                return text_reply(answer, "unknown important question", handoff_required=True, handoff_reason="missing_knowledge")
        return text_reply(answer, "interrupt answered, waiting before returning to active question")

    if semantic.message_type == "partial_answer":
        merged_profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
        if stage_requirement_met(stage, merged_profile):
            return ReplyResult(send_reply=True, outgoing_messages=[], reply_text=None, summary="partial completed stage", confidence=0.85)
        return text_reply(partial_followup(stage, state, semantic) or current_question, "partial answer")

    return ReplyResult(send_reply=True, outgoing_messages=[], reply_text=None, summary="controller will continue", confidence=0.8)


def interrupt_timeout_reply(state: FunnelGraphState) -> ReplyResult | None:
    metadata = dict(state.get("metadata") or {})
    if state.get("timeout_event") != "interrupt_followup":
        return None
    if not metadata.get("awaiting_interrupt_followup"):
        return None
    question = str(metadata.get("interrupt_followup_question") or state.get("pending_question_text") or state.get("current_question") or "").strip()
    if not question:
        return None
    return text_reply(natural_timeout_followup(question, metadata), "interrupt followup timeout")


def natural_timeout_followup(question: str, metadata: dict[str, Any]) -> str:
    count = int(metadata.get("interrupt_followup_count") or 0)
    variants = question_variants(question)
    return variants[count % len(variants)] if variants else question


def question_variants(question: str) -> list[str]:
    variants_by_question = {
        "Остались ли у тебя какие-нибудь ещё вопросы?": [
            "Что-то ещё осталось непонятным?",
            "Если вопросов больше нет, можем двигаться дальше.",
            "Ещё что-то хочешь уточнить по условиям или формату?",
        ],
        "Рассказать подробнее?": [
            "Хочешь, расскажу подробнее?",
            "Интересно узнать детали?",
            "Могу рассказать подробнее, если актуально.",
        ],
        "Какая у тебя модель телефона?": [
            "Классно, что с оборудованием уже есть база. Для старта всё равно нужна модель телефона — какая у тебя?",
            "Поняла про оборудование. А модель телефона какая?",
            "Супер, это плюс. Подскажи тогда модель телефона.",
        ],
        "Расскажи немного о себе: учишься/работаешь? Чем любишь заниматься в свободное время?": [
            "Расскажешь немного о себе: учишься или работаешь, чем любишь заниматься?",
            "А по себе подскажи, пожалуйста: учёба/работа и что нравится в свободное время?",
            "Чтобы подобрать тематику, расскажи пару слов о себе: учишься/работаешь, чем увлекаешься?",
        ],
        "Если интересна наша сфера, давай расскажу про зп и график": [
            "Если по формату стало понятнее, рассказать про зп и график?",
            "Могу дальше рассказать про зарплату и график, интересно?",
            "Хочешь, перейду к зп и графику?",
        ],
    }
    return variants_by_question.get(question, [question])


def text_reply(
    text: str,
    summary: str,
    *,
    handoff_required: bool = False,
    handoff_reason: str | None = None,
) -> ReplyResult:
    return ReplyResult(
        send_reply=True,
        outgoing_messages=[ReplyOutgoingMessage(type="text", text=text)] if text else [],
        reply_text=text or None,
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        summary=summary,
        confidence=0.85,
    )


def knowledge_answer(state: FunnelGraphState) -> str:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    wanted_topics = {str(topic) for topic in semantic.retrieval_topics or [] if topic}
    faq_items = list(state.get("faq_context") or [])
    objection_items = list(state.get("objection_context") or [])
    selected: list[dict[str, Any]] = []
    if wanted_topics:
        selected.extend([item for item in faq_items if str(item.get("topic") or "") in wanted_topics])
        selected.extend([item for item in objection_items if str(item.get("topic") or "") in wanted_topics])
    if not selected and faq_items:
        selected.append(faq_items[0])
    if semantic.interrupt_type == "objection" and objection_items and objection_items[0] not in selected:
        selected.append(objection_items[0])

    answers = []
    for item in selected[:2]:
        answer = str(item.get("answer") or item.get("content") or "").strip()
        if answer and answer not in answers:
            answers.append(answer)
    return " ".join(answers)


def partial_followup(stage: str, state: FunnelGraphState, semantic: SemanticResult) -> str | None:
    profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
    if stage == "contact_collection":
        if profile.get("phone_number") and not profile.get("candidate_name"):
            return "Спасибо, номер получила. Напиши, пожалуйста, имя."
        if profile.get("candidate_name") and not profile.get("phone_number"):
            return "Спасибо. Теперь пришли, пожалуйста, номер телефона для записи."
    if stage == "equipment_phone_check" and profile.get("equipment_available") and not profile.get("phone_model"):
        return "О, круто! А чтобы мы точно всё настроили — какая у тебя модель телефона?"
    if stage == "interview_custom_time":
        if profile.get("interview_day") and not profile.get("interview_time"):
            return "Хорошо, а по времени когда удобно?"
        if profile.get("interview_time") and not profile.get("interview_day"):
            return "По времени поняла. На какой день записать?"
    if stage == "interview_time_check":
        return "На это время может не быть слота. Подскажи, пожалуйста, время с 11:00 по 18:00."
    if stage == "room_available_check":
        return "Поняла. А получится организовать место, где во время эфира тебе никто не будет мешать?"
    return None


def non_null_facts(semantic: SemanticResult) -> dict[str, Any]:
    return {key: value for key, value in semantic.facts.model_dump().items() if value is not None}


def join_text(first: str | None, second: str | None) -> str:
    parts = [part.strip() for part in (first, second) if part and part.strip()]
    return "\n\n".join(parts)


def reply_to_json(result: ReplyResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
