from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.funnel_policy import STAGE_POLICIES, TERMINAL_STAGES, get_stage_policy, stage_requirement_met
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.model_profiles import active_model_profile
from app.services.funnel_graph.persona import with_persona
from app.services.funnel_graph.style_examples import select_style_examples
from app.services.funnel_graph.semantic import (
    GENERIC_TOPICS,
    SemanticResult,
    has_specific_topic,
    is_recoverable_llm_format_error,
    normalize_text,
    repair_mojibake,
)
from app.services.funnel_graph.state import FunnelGraphState


PROMPT_PATH = PROJECT_ROOT / "prompts" / "reply_orchestrator.md"
ReplyMode = Literal["answer_only", "answer_and_soft_return", "ask_missing_field", "no_reply", "delay_then_answer"]
DELAY_MIN_SECONDS = 15
DELAY_MAX_SECONDS = 45
DELAY_DEFAULT_SECONDS = 25
MAX_TEXT_MESSAGES = 3
MAX_TOTAL_REPLY_SENTENCES = 3
MAX_SENTENCES_PER_TEXT_MESSAGE = 2
ENGLISH_LEVEL_REPLY = (
    "английский не обязателен, работаем с переводчиком, плюс оператор подсказывает. "
    "если уровень слабый, это норм, всё объяснят до старта"
)
SOFT_RETURN_REPLIES = {
    "interest_check": "поняла) чтобы не грузить всем сразу — тебе в целом интересно продолжить и узнать условия?",
    "post_equipment_questions_check": "поняла) чтобы не грузить всем сразу — если по условиям в целом понятно, можем двигаться дальше?",
    "profile_theme_check": "поняла) чтобы не грузить всем сразу — расскажешь пару слов о себе?",
    "equipment_phone_check": "поняла) чтобы не грузить всем сразу — вернёмся к телефончику, какая у тебя моделька?",
    "interview_offer": "поняла) чтобы не грузить всем сразу — в целом готова записаться на собеседование?",
}


class ReplyOutgoingMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "text"
    text: str | None = None
    voice_pack_id: str | None = None
    template_id: str | None = None
    delay_seconds: int | None = None


class ReplyResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    send_reply: bool = True
    reply_mode: ReplyMode = "answer_only"
    outgoing_messages: list[ReplyOutgoingMessage] = Field(default_factory=list)
    reply_text: str | None = None
    handoff_required: bool = False
    handoff_reason: str | None = None
    summary: str = ""
    confidence: float = 0.0

    @field_validator("reply_mode", mode="before")
    @classmethod
    def normalize_reply_mode(cls, value: Any) -> str:
        allowed = {"answer_only", "answer_and_soft_return", "ask_missing_field", "no_reply", "delay_then_answer"}
        if value in (None, "", []):
            return "answer_only"
        normalized = str(value).strip()
        return normalized if normalized in allowed else "answer_only"


class ReactionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = ""


SMALLTALK_REACTION_PROMPT = """Кандидатка только что в ответ на просьбу рассказать о себе написала пару слов про учёбу/работу/чем занимается в свободное время.
Твоя задача: коротко и ЖИВО отреагировать именно на то, что она написала — по-человечески, тепло, с интересом.

Правила:
- Ровно 1 короткое предложение. Без вопроса в конце (следующий вопрос добавят отдельно).
- Реагируй на конкретику её сообщения. Например: «смотрю тиктоки» — оживись про короткие форматы/тренды и что под это легко подобрать тему стримов; «ничего не интересно/ничем не занимаюсь» — поддержи без осуждения и мягко свяжи со стримами.
- Голос живой, на «ты», без канцелярита. НЕЛЬЗЯ отвечать сухими «поняла, спасибо» / «это поможет подобрать тематику».
- Без обещаний дохода, без давления, без Markdown.
- Верни только валидный JSON: {"text": "..."}.
"""


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
        self.last_run_metadata: dict[str, Any] = {}

    async def run(self, state: FunnelGraphState) -> ReplyResult:
        started = time.perf_counter()
        profile = active_model_profile(self.settings)
        semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
        if semantic.message_type == "empty":
            result = deterministic_reply(state)
            self.last_run_metadata = reply_metadata(profile.name, "deterministic", None, started, True, "empty")
            return result
        if profile.use_fast_path and reply_fast_path_allowed(state, semantic):
            result = deterministic_reply(state)
            self.last_run_metadata = reply_metadata(
                profile.name,
                "deterministic",
                None,
                started,
                True,
                "completed_by_policy",
            )
            return result
        # Always prefer the best (max) model for live reactions when the profile exposes a max
        # route. Quality of the reaction matters more than latency for this recruiter funnel.
        prefer_complex = profile.route_complex_to_max and self.adapter.has_api_key("reply_orchestrator_complex")
        component = "reply_orchestrator_complex" if prefer_complex else "reply_orchestrator"
        if self.use_llm and self.adapter.has_api_key(component):
            try:
                payload = await self.adapter.complete_json(
                    component=component,
                    system_prompt=with_persona(self.prompt_path.read_text(encoding="utf-8")),
                    user_payload=reply_llm_payload(state, semantic),
                    response_model=ReplyResult,
                )
                parsed = sanitize_reply_result(state, ReplyResult.model_validate(payload))
                guarded = guard_reply_with_policy(state, parsed)
                if guarded is not None:
                    self.last_run_metadata = reply_metadata(
                        profile.name,
                        component,
                        self.adapter.config_for(component, ReplyResult).model,
                        started,
                        False,
                        "guarded",
                    )
                    return guarded
                if parsed.outgoing_messages or parsed.send_reply is False:
                    self.last_run_metadata = reply_metadata(
                        profile.name,
                        component,
                        self.adapter.config_for(component, ReplyResult).model,
                        started,
                        False,
                        "completed",
                    )
                    return parsed
            except BrainLLMError as exc:
                fallback = await self._try_complex_fallback(state, semantic, component, started, str(exc))
                if fallback is not None:
                    return fallback
                if not (self.fallback_on_llm_error or is_recoverable_llm_format_error(exc)):
                    raise
            except (ValidationError, ValueError, TypeError):
                fallback = await self._try_complex_fallback(state, semantic, component, started, "invalid_json")
                if fallback is not None:
                    return fallback
                pass
            except Exception:
                result = deterministic_reply(state)
                self.last_run_metadata = reply_metadata(profile.name, "deterministic", None, started, True, "fallback_exception")
                return result
        result = deterministic_reply(state)
        self.last_run_metadata = reply_metadata(profile.name, "deterministic", None, started, True, "fallback")
        return result

    async def _try_complex_fallback(
        self,
        state: FunnelGraphState,
        semantic: SemanticResult,
        component: str,
        started: float,
        reason: str,
    ) -> ReplyResult | None:
        profile = active_model_profile(self.settings)
        fallback_component = "reply_orchestrator_complex"
        if not profile.route_complex_to_max or component == fallback_component or not self.adapter.has_api_key(fallback_component):
            return None
        try:
            payload = await self.adapter.complete_json(
                component=fallback_component,
                system_prompt=with_persona(self.prompt_path.read_text(encoding="utf-8")),
                user_payload=reply_llm_payload(state, semantic),
                response_model=ReplyResult,
            )
            parsed = sanitize_reply_result(state, ReplyResult.model_validate(payload))
            guarded = guard_reply_with_policy(state, parsed)
            if guarded is not None:
                result = guarded
            elif parsed.outgoing_messages or parsed.send_reply is False:
                result = parsed
            else:
                result = deterministic_reply(state)
            self.last_run_metadata = reply_metadata(
                profile.name,
                fallback_component,
                self.adapter.config_for(fallback_component, ReplyResult).model,
                started,
                False,
                "fallback_completed",
                fallback_reason=reason,
            )
            return result
        except Exception:
            return None

    async def generate_smalltalk_reaction(self, state: FunnelGraphState) -> str | None:
        """Live, LLM-generated reaction to what the candidate shared about herself.

        Returns None if the LLM is unavailable or fails, so the caller can fall back to a
        deterministic ack. Always uses the best (max) model when the profile exposes one.
        """
        if not self.use_llm:
            return None
        profile = active_model_profile(self.settings)
        prefer_complex = profile.route_complex_to_max and self.adapter.has_api_key("reply_orchestrator_complex")
        component = "reply_orchestrator_complex" if prefer_complex else "reply_orchestrator"
        if not self.adapter.has_api_key(component):
            return None
        candidate_profile = dict(state.get("candidate_profile") or {})
        user_payload = {
            "candidate_message": state.get("incoming_message"),
            "profile_info": candidate_profile.get("profile_info"),
            "work_or_study": candidate_profile.get("work_or_study"),
            "hobbies": candidate_profile.get("hobbies"),
            "recent_messages": compact_messages(state.get("recent_messages") or [], limit=10),
            "style_examples": select_style_examples("support_smalltalk", [], k=3),
        }
        try:
            payload = await self.adapter.complete_json(
                component=component,
                system_prompt=with_persona(SMALLTALK_REACTION_PROMPT),
                user_payload=user_payload,
                response_model=ReactionResult,
            )
            text = normalize_reply_message_text(ReactionResult.model_validate(payload).text)
            return text or None
        except Exception:
            return None


def reply_fast_path_allowed(state: FunnelGraphState, semantic: SemanticResult) -> bool:
    if semantic.current_goal_satisfied and not semantic.has_unresolved_interrupt:
        return True
    if semantic.message_type in {"pause", "do_not_contact", "hard_refusal"} and semantic.confidence >= 0.85:
        return True
    if semantic.message_type == "unclear" and not semantic.has_unresolved_interrupt:
        # Neutral acks during an interrupt-followup wait stay deterministic (we just wait).
        # Other short/unclear turns go to the LLM so it can re-anchor naturally instead of
        # repeating the canned stage question word-for-word.
        return should_wait_after_neutral_ack(state, semantic)
    return False


def reply_metadata(
    profile: str,
    component: str | None,
    model: str | None,
    started: float,
    fast_path: bool,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "component": component,
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "fast_path": fast_path,
        "status": status,
        **extra,
    }


def reply_llm_payload(state: FunnelGraphState, semantic: SemanticResult | None = None) -> dict[str, Any]:
    semantic = semantic or SemanticResult.model_validate(state.get("semantic_result") or {})
    stage = str(state.get("stage") or "interest_check")
    policy = get_stage_policy(stage)
    pending_question = str(state.get("pending_question_text") or state.get("current_question") or "")
    metadata = dict(state.get("metadata") or {})
    payload: dict[str, Any] = {
        "role_contract": {
            "layer": "final_reply_generator",
            "semantic_is_classifier_only": True,
            "retrieval_is_options_only": True,
            "state_controller_moves_stage": True,
        },
        "reply_mode_contract": {
            "allowed_modes": ["answer_only", "answer_and_soft_return", "ask_missing_field", "no_reply", "delay_then_answer"],
            "max_text_messages": MAX_TEXT_MESSAGES,
            "max_total_reply_sentences": MAX_TOTAL_REPLY_SENTENCES,
            "max_sentences_per_text_message": MAX_SENTENCES_PER_TEXT_MESSAGE,
            "message_splitting": "Prefer several short outgoing_messages when there are separate ideas. Each text message must contain 1-2 sentences, and the whole logical reply must contain no more than 3 sentences.",
            "ending_punctuation": "Do not end text messages with a final period. A question mark is allowed only for a real question.",
            "delay_seconds_range": [DELAY_MIN_SECONDS, DELAY_MAX_SECONDS],
            "default_delay_seconds": DELAY_DEFAULT_SECONDS,
        },
        "stage_contract": {
            "current_state": stage,
            "current_goal": state.get("current_goal") or policy.goal,
            "pending_question": pending_question,
            "next_stage_if_completed": state.get("next_stage_if_completed") or policy.next_stage_if_completed,
            "required_fields": list(policy.required_fields),
            "allowed_transitions": list(policy.allowed_transitions),
        },
        "candidate_state": state.get("candidate_profile") or {},
        "incoming_message": state.get("incoming_message"),
        "message_batch": compact_messages(state.get("message_batch") or [], limit=12),
        "dialogue_context": {
            "recent_messages": compact_messages(state.get("recent_messages") or [], limit=30),
            "conversation_history": compact_messages(state.get("conversation_history") or [], limit=50),
            "last_bot_message": state.get("last_bot_message") or metadata.get("last_bot_message"),
            "last_user_message": state.get("last_user_message") or metadata.get("last_user_message"),
        },
        "semantic_result": semantic.model_dump(),
        "style_examples": select_style_examples(
            stage,
            [*(semantic.retrieval_topics or []), *([semantic.interrupt_topic] if semantic.interrupt_topic else [])],
            k=3,
        ),
        "knowledge_options": knowledge_options(state),
        "retrieval_debug": state.get("retrieved_knowledge") or {},
        "response_rules": state.get("response_rules") or {},
        "interrupt_followup": {
            "awaiting": bool(metadata.get("awaiting_interrupt_followup")),
            "question": metadata.get("interrupt_followup_question"),
            "count": metadata.get("interrupt_followup_count") or 0,
            "streak_stage": metadata.get("interrupt_streak_stage"),
            "streak_count": metadata.get("interrupt_streak_count") or 0,
        },
    }
    if stage == "inbound_warmup":
        # Она написала ПЕРВОЙ. Сейчас только тёплый разговор: поздороваться, ответить
        # по-человечески, поддержать беседу. Про работу/стриминг — НЕ в этом сообщении:
        # предложение мы добавим сами позже, когда беседа затихнет.
        payload["warmup_directive"] = {
            "active": True,
            "situation": "Кандидатка написала тебе первой и о чём-то спрашивает или просто болтает.",
            "do": (
                "Поздоровайся тепло и по-человечески, ответь по смыслу на её сообщение/вопросы. "
                "Личные детали о себе можно придумать правдоподобно (см. персону: родом из небольшой деревни "
                "примерно в 100 км, та ещё дыра), недавно переехала; просто будь живой девочкой). "
                "Поддержи лёгкую беседу, не допрашивай."
            ),
            "must_not": (
                "НЕ предлагай работу, НЕ упоминай стриминг/вакансию/предложение в этом сообщении — "
                "это произойдёт чуть позже само. Факты про условия работы (оплата, график, платформы) "
                "по-прежнему не выдумывай."
            ),
        }
    return payload


def compact_messages(messages: list[Any], *, limit: int) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for raw in messages[-limit:]:
        item = dict(raw or {})
        text = str(item.get("body") or item.get("text") or "").strip()
        compacted.append(
            {
                "direction": item.get("direction"),
                "sender_type": item.get("sender_type"),
                "body": text[:1200],
                "at": item.get("at") or item.get("sent_at"),
            }
        )
    return compacted


def knowledge_options(state: FunnelGraphState) -> list[dict[str, Any]]:
    wanted_topics = wanted_knowledge_topics(state)
    options: list[dict[str, Any]] = []
    for source, items in (
        ("faq_context", state.get("faq_context") or []),
        ("objection_context", state.get("objection_context") or []),
    ):
        for index, item in enumerate(items):
            raw = dict(item or {})
            content = str(raw.get("answer") or raw.get("content") or "").strip()
            if not content:
                continue
            topic = str(raw.get("topic") or "")
            if wanted_topics and topic not in wanted_topics:
                continue
            options.append(
                {
                    "source": source,
                    "rank": index + 1,
                    "topic": raw.get("topic"),
                    "content": content[:1600],
                    "score": raw.get("score"),
                    "match_reasons": raw.get("match_reasons") or raw.get("reasons") or [],
                }
            )
    retrieved = dict(state.get("retrieved_knowledge") or {})
    for index, card in enumerate(retrieved.get("cards") or []):
        raw = dict(card or {})
        content = str(raw.get("content") or raw.get("answer") or "").strip()
        if not content:
            continue
        topic = str(raw.get("topic") or raw.get("card_key") or "")
        if wanted_topics and topic not in wanted_topics:
            continue
        options.append(
            {
                "source": "knowledge_cards",
                "rank": index + 1,
                "topic": raw.get("topic") or raw.get("card_key"),
                "content": content[:1600],
                "score": raw.get("score"),
                "match_reasons": raw.get("match_reasons") or [],
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for option in options:
        key = (str(option.get("topic") or ""), str(option.get("content") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(option)
    return deduped[:12]


def wanted_knowledge_topics(state: FunnelGraphState) -> set[str]:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    topics = {str(topic) for topic in semantic.retrieval_topics or [] if topic and str(topic) not in GENERIC_TOPICS}
    if semantic.interrupt_topic and semantic.interrupt_topic not in GENERIC_TOPICS:
        topics.add(str(semantic.interrupt_topic))
    if "nudity_onlyfans" in topics:
        topics.add("nudity_concern")
    if "nudity_concern" in topics:
        topics.add("nudity_onlyfans")
    return topics


def sanitize_reply_result(state: FunnelGraphState, parsed: ReplyResult) -> ReplyResult:
    stage = str(state.get("stage") or "interest_check")
    policy = get_stage_policy(stage)
    delay_allowed = stage not in TERMINAL_STAGES and policy.stage_type != "action"
    mode: ReplyMode = parsed.reply_mode
    if parsed.send_reply is False or mode == "no_reply":
        return ReplyResult(
            send_reply=False,
            reply_mode="no_reply",
            outgoing_messages=[],
            reply_text=None,
            handoff_required=parsed.handoff_required,
            handoff_reason=parsed.handoff_reason,
            summary=parsed.summary,
            confidence=parsed.confidence,
        )
    if mode == "delay_then_answer" and not delay_allowed:
        mode = "answer_only"

    messages: list[ReplyOutgoingMessage] = []
    text_count = 0
    for message in parsed.outgoing_messages:
        if message.type == "text":
            text = normalize_reply_message_text(str(message.text or ""))
            if not text or text_count >= MAX_TEXT_MESSAGES:
                continue
            text_count += 1
            delay_seconds = None
            if mode == "delay_then_answer":
                delay_seconds = clamp_delay_seconds(message.delay_seconds)
            messages.append(
                ReplyOutgoingMessage(
                    type="text",
                    text=text,
                    voice_pack_id=message.voice_pack_id,
                    template_id=message.template_id,
                    delay_seconds=delay_seconds,
                )
            )
            continue
        messages.append(
            ReplyOutgoingMessage(
                type=message.type,
                text=message.text,
                voice_pack_id=message.voice_pack_id,
                template_id=message.template_id,
                delay_seconds=None,
            )
        )

    reply_text = "\n\n".join(message.text or "" for message in messages if message.type == "text").strip()
    if not reply_text and parsed.reply_text:
        reply_text = normalize_reply_message_text(str(parsed.reply_text))
    return ReplyResult(
        send_reply=parsed.send_reply,
        reply_mode=mode,
        outgoing_messages=messages,
        reply_text=reply_text,
        handoff_required=parsed.handoff_required,
        handoff_reason=parsed.handoff_reason,
        summary=parsed.summary,
        confidence=parsed.confidence,
    )


def clamp_delay_seconds(value: int | None) -> int:
    if value is None:
        return DELAY_DEFAULT_SECONDS
    return max(DELAY_MIN_SECONDS, min(DELAY_MAX_SECONDS, int(value)))


def normalize_reply_message_text(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    while cleaned.endswith("."):
        cleaned = cleaned[:-1].rstrip()
    return cleaned


def guard_reply_with_policy(state: FunnelGraphState, parsed: ReplyResult) -> ReplyResult | None:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    if semantic.message_type == "empty":
        return None
    proactive_followup = interview_booking_missing_field_followup(state, semantic)
    if proactive_followup:
        return text_reply(
            proactive_followup,
            "booking intent missing field followup",
            reply_mode="ask_missing_field",
            handoff_required=parsed.handoff_required,
            handoff_reason=parsed.handoff_reason,
        )
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
            if not followup:
                followup = str(state.get("pending_question_text") or state.get("current_question") or "").strip()
            if followup:
                return text_reply(
                    followup,
                    "policy partial followup",
                    reply_mode="ask_missing_field",
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

    if current_question and current_question in outgoing_text:
        return deterministic_reply(state)
    if contains_other_stage_question(outgoing_text, current_question, stage):
        return deterministic_reply(state)
    return None


def interview_booking_missing_field_followup(state: FunnelGraphState, semantic: SemanticResult) -> str | None:
    if semantic.has_unresolved_interrupt or semantic.current_goal_satisfied:
        return None
    if semantic.facts.interview_interest is not True:
        return None
    stage = str(state.get("stage") or "interest_check")
    merged_profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
    if stage_requirement_met(stage, merged_profile):
        return None
    followup = partial_followup(stage, state, semantic)
    return followup or str(state.get("pending_question_text") or state.get("current_question") or "").strip() or None


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


def has_prior_agent_message(state: FunnelGraphState) -> bool:
    history = list(state.get("conversation_history") or []) + list(state.get("recent_messages") or [])
    for item in history:
        if not isinstance(item, dict):
            continue
        direction = str(item.get("direction") or "").lower()
        sender = str(item.get("sender_type") or "").lower()
        if direction == "outbound" or sender in {"agent", "bot", "recruiter"}:
            return True
    return False


def deterministic_reply(state: FunnelGraphState) -> ReplyResult:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    stage = str(state.get("stage") or "interest_check")
    current_question = str(state.get("pending_question_text") or state.get("current_question") or "")
    templates = dict((state.get("metadata") or {}).get("templates") or {})

    # Inbound-first warmup без LLM: просто поздороваться и мягко ответить, если есть
    # что. Питч добавит контроллер при переходе warmup→interest_check.
    if stage == "inbound_warmup":
        greet = "привет)" if not has_prior_agent_message(state) else ""
        answer = knowledge_answer(state)
        text = join_text(greet, answer) or greet or "привет) рада, что написала"
        return text_reply(text, "inbound warmup (deterministic)")

    # Первый контакт (мы ещё ни разу не писали в диалог): кто бы ни написал первым
    # — «привет», вопрос, что угодно — открываем тем же first-touch опенером, что и
    # все остальные лиды, а не вопросом из середины воронки. Исключение —
    # явная просьба не писать.
    if (
        stage == "interest_check"
        and not has_prior_agent_message(state)
        and semantic.message_type != "do_not_contact"
    ):
        first_touch = str(
            (state.get("retrieved_knowledge") or {}).get("first_touch_message")
            or templates.get("first_touch_message")
            or ""
        )
        if first_touch:
            return text_reply(first_touch, "first touch (inbound-first contact)")

    if semantic.message_type == "empty":
        timeout_reply = interrupt_timeout_reply(state)
        if timeout_reply is not None:
            return timeout_reply
        if state.get("timeout_event"):
            return ReplyResult(send_reply=False, reply_mode="no_reply", summary="empty timeout event", confidence=1.0)
        first_touch = str((state.get("retrieved_knowledge") or {}).get("first_touch_message") or templates.get("first_touch_message") or "")
        return text_reply(first_touch or current_question, "first touch")
    if semantic.message_type == "do_not_contact":
        return ReplyResult(send_reply=False, reply_mode="no_reply", summary="do not contact", confidence=1.0)
    if semantic.message_type == "hard_refusal":
        return text_reply(str(templates.get("lost_message") or "поняла, не буду отвлекать) хорошего дня"), "hard refusal")
    if semantic.message_type == "pause":
        return text_reply("хорошо, буду ждать", "pause")

    if should_wait_after_neutral_ack(state, semantic):
        return ReplyResult(send_reply=False, reply_mode="no_reply", summary="neutral acknowledgement after answer, wait", confidence=0.9)

    answer = knowledge_answer(state)
    if semantic.has_unresolved_interrupt:
        if is_generic_unclear_interrupt(semantic):
            incoming = repair_mojibake(str(state.get("incoming_message") or "")).strip().lower()
            if stage == "interest_check" and incoming in {"привет", "приветик", "здравствуйте", "добрый день"}:
                return text_reply(current_question, "social acknowledgement, continue current question")
            if stage == "post_equipment_questions_check":
                return text_reply(
                    "поняла) тогда уточню: остались ли у тебя ещё вопросики по условиям, оплате или формату?",
                    "natural questions followup",
                )
            clarify = "не совсем поняла, уточни, пожалуйста"
            return text_reply(f"{clarify}) {current_question}" if current_question else clarify, "unclear")
        if has_topic(semantic, "english_level") and (not answer or wanted_knowledge_topics(state) <= {"english_level"}):
            answer = ENGLISH_LEVEL_REPLY
        if not answer:
            answer = unknown_interrupt_reply(stage, current_question)
        if should_soft_return_to_goal(state, semantic):
            answer = join_text(answer, soft_return_reply(stage))
        return text_reply(answer, "interrupt answered, waiting before returning to active question")

    if semantic.message_type == "partial_answer":
        merged_profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
        if stage_requirement_met(stage, merged_profile):
            return ReplyResult(send_reply=True, outgoing_messages=[], reply_text=None, summary="partial completed stage", confidence=0.85)
        return text_reply(partial_followup(stage, state, semantic) or current_question, "partial answer", reply_mode="ask_missing_field")

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


# Питч про стриминг для написавших ПЕРВЫМИ (см. inbound_warmup в funnel_policy и
# TRANSITION_BRIDGES в graph.py). Живёт здесь, а не в graph.py, чтобы варианты
# лежали рядом в QUESTION_VARIANTS без циклического импорта.
INBOUND_WARMUP_PITCH = "кстати ты очень фотогеничная) есть предложение по работе в стриминге, если интересно — расскажу"

PROFILE_BRIDGE = "давай я уточню у тебя несколько деталей, и далее мы с тобой запишемся на собеседование"

QUESTION_VARIANTS: dict[str, list[str]] = {
        "остались ли у тебя какие-нибудь ещё вопросики?": [
            "что-то ещё осталось непонятным?",
            "если вопросиков больше нет, можем двигаться дальше",
            "ещё что-то хочешь уточнить по условиям или формату?",
        ],
        "если интересно — расскажу, что за работа и как всё устроено 🙂": [
            "если хочешь, расскажу про условия и график — что да как)",
            "могу рассказать поподробнее, что за работа и сколько выходит — интересно?",
            "давай расскажу, что за формат и какие условия — займёт минутку)",
        ],
        INBOUND_WARMUP_PITCH: [
            "слушай, ты прям хорошо смотришься в кадре) у нас есть работа в стриминге — рассказать, что за движ?",
            "кстати, есть тема по работе на разговорных стримах, по вайбу ты подходишь) интересно?",
            "у меня к тебе предложение по работе в стриминге — без вебкама, просто эфиры) рассказать подробнее?",
        ],
        PROFILE_BRIDGE: [
            "давай уточню пару моментов о тебе — и сможем записаться на собеседование",
            "осталось узнать немного деталей, и можно договариваться о собеседовании)",
            "ещё пара вопросиков о тебе, и перейдём к записи на собеседование",
        ],
        "какая у тебя моделька телефончика?": [
            "классно, что с оборудованием уже есть база) для старта всё равно нужна моделька телефончика — какая у тебя?",
            "поняла про оборудование) а моделька телефончика какая?",
            "супер, это плюс) подскажи тогда модельку телефончика",
        ],
        "расскажи немного о себе, учишься/работаешь? чем любишь заниматься в свободное время? помогу подобрать тематику для стримов 🐬": [
            "расскажешь немного о себе: учишься или работаешь, чем любишь заниматься?",
            "а по себе подскажи, пожалуйста: учёба/работа и что нравится в свободное время?",
            "чтобы подобрать тематику, расскажи пару слов о себе: учишься/работаешь, чем увлекаешься?",
        ],
        "если интересна наша сфера, давай расскажу про зп и график 🐬": [
            "если по формату стало понятнее, рассказать про зп и график?",
            "могу дальше рассказать про зарплату и график, интересно?",
            "хочешь, перейду к зп и графику?",
        ],
        "тогда можем записаться на собеседование?": [
            "давай назначим короткий созвон-собеседование?",
            "получится созвониться на собеседование?",
            "как смотришь на то, чтобы созвониться с куратором и всё обсудить?",
        ],
        "завтра будет удобно провести собеседование?": [
            "получится завтра созвониться на собеседование?",
            "как тебе вариант провести собеседование завтра?",
            "завтра сможем выделить время на созвон?",
        ],
        "с 11:00 по 18:00 по мск, в какое время будет удобнее?": [
            "подскажи удобное время в промежутке с 11:00 до 18:00 мск?",
            "во сколько тебе комфортнее в окне 11:00–18:00 по мск?",
            "какое время с 11 до 18 по мск тебе подойдёт?",
        ],
}


def question_variants(question: str) -> list[str]:
    return QUESTION_VARIANTS.get(question, [question])


_CANONICAL_BY_VARIANT: dict[str, str] = {
    variant: canonical
    for canonical, variants in QUESTION_VARIANTS.items()
    for variant in [canonical, *variants]
}


def canonical_question(text: str) -> str:
    """Свести (возможно переформулированный) вопрос к его канонной форме, чтобы
    отличать повтор одного и того же вопроса от нового, даже если фраза менялась."""
    return _CANONICAL_BY_VARIANT.get((text or "").strip(), (text or "").strip())


def should_wait_after_neutral_ack(state: FunnelGraphState, semantic: SemanticResult) -> bool:
    metadata = dict(state.get("metadata") or {})
    if not metadata.get("awaiting_interrupt_followup"):
        return False
    if semantic.has_unresolved_interrupt or semantic.message_type != "unclear":
        return False
    return is_neutral_ack_text(str(state.get("incoming_message") or ""))


def is_neutral_ack_text(text: str) -> bool:
    normalized = normalize_text(text)
    cleaned = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    tokens = [token for token in cleaned.split() if token]
    if not tokens or len(tokens) > 3:
        return False
    ack_tokens = {
        "спасибо",
        "спс",
        "поняла",
        "понял",
        "ясно",
        "ага",
        "ок",
        "окей",
        "понятно",
    }
    return all(token in ack_tokens for token in tokens)


def should_soft_return_to_goal(state: FunnelGraphState, semantic: SemanticResult) -> bool:
    if semantic.interrupt_type not in {"question", "objection"}:
        return False
    stage = str(state.get("stage") or "interest_check")
    metadata = dict(state.get("metadata") or {})
    if metadata.get("interrupt_streak_stage") not in {None, stage}:
        return False
    if not str(state.get("pending_question_text") or state.get("current_question") or "").strip():
        return False
    return int(metadata.get("interrupt_streak_count") or 0) >= 3


def soft_return_reply(stage: str) -> str:
    return SOFT_RETURN_REPLIES.get(
        stage,
        "поняла) чтобы не грузить всем сразу — давай вернёмся к текущему шагу, хорошо?",
    )


def has_topic(semantic: SemanticResult, topic: str) -> bool:
    values = [semantic.interrupt_topic, *list(semantic.retrieval_topics or [])]
    return any(str(value or "").strip().lower() == topic for value in values)


def unknown_interrupt_reply(stage: str, current_question: str) -> str:
    if stage in {"contact_collection", "interview_day_check", "interview_time_check", "interview_custom_time"}:
        return "не хочу придумывать наугад) давай пока зафиксируем запись, а детали спокойно разберём дальше"
    if current_question:
        return "точных данных по этому пункту у меня нет) лучше разобрать это на собеседовании, чтобы не сказать лишнего"
    return "точных данных по этому пункту у меня нет) лучше уточнить это на собеседовании, чтобы не сказать лишнего"


def text_reply(
    text: str,
    summary: str,
    *,
    reply_mode: ReplyMode = "answer_only",
    handoff_required: bool = False,
    handoff_reason: str | None = None,
) -> ReplyResult:
    return ReplyResult(
        send_reply=True,
        reply_mode=reply_mode,
        outgoing_messages=[ReplyOutgoingMessage(type="text", text=normalize_reply_message_text(text))] if text else [],
        reply_text=normalize_reply_message_text(text) or None,
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        summary=summary,
        confidence=0.85,
    )


def knowledge_answer(state: FunnelGraphState) -> str:
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    wanted_topics = {str(topic) for topic in semantic.retrieval_topics or [] if topic and str(topic) not in GENERIC_TOPICS}
    if semantic.interrupt_topic and semantic.interrupt_topic not in GENERIC_TOPICS:
        wanted_topics.add(str(semantic.interrupt_topic))
    faq_items = list(state.get("faq_context") or [])
    objection_items = list(state.get("objection_context") or [])
    selected: list[dict[str, Any]] = []
    if wanted_topics:
        selected.extend([item for item in faq_items if str(item.get("topic") or "") in wanted_topics])
        selected.extend([item for item in objection_items if str(item.get("topic") or "") in wanted_topics])
    if semantic.interrupt_type == "objection":
        matching_objections = [item for item in objection_items if str(item.get("topic") or "") in wanted_topics]
        for item in matching_objections:
            if item not in selected:
                selected.append(item)
        if not selected and objection_items:
            selected.append(objection_items[0])

    answers = []
    for item in selected[:MAX_TEXT_MESSAGES]:
        answer = str(item.get("answer") or item.get("content") or "").strip()
        if answer and answer not in answers:
            answers.append(answer)
    return " ".join(answers)


def is_generic_unclear_interrupt(semantic: SemanticResult) -> bool:
    return semantic.interrupt_type in {"none", "unclear"} and not has_specific_topic(semantic)


def partial_followup(stage: str, state: FunnelGraphState, semantic: SemanticResult) -> str | None:
    profile = {**dict(state.get("candidate_profile") or {}), **non_null_facts(semantic)}
    if stage == "contact_collection":
        if profile.get("phone_number") and not profile.get("candidate_name"):
            return "спасибо, номер получила) напиши, пожалуйста, имя"
        if profile.get("candidate_name") and not profile.get("phone_number"):
            return "спасибо) теперь пришли, пожалуйста, номер телефончика для записи"
        if profile.get("interview_interest"):
            return "супер) тогда для записи пришли, пожалуйста, имя и номер телефончика"
    if stage == "equipment_phone_check" and profile.get("equipment_available") and not profile.get("phone_model"):
        return "о, круто! а чтобы мы точно всё настроили, какая у тебя моделька телефончика?"
    if stage == "interview_custom_time":
        if profile.get("interview_day") and not profile.get("interview_time"):
            return "хорошо, а по времени когда удобно?"
        if profile.get("interview_time") and not profile.get("interview_day"):
            return "по времени поняла) на какой день записать?"
    if stage == "interview_time_check":
        if profile.get("interview_interest"):
            return "супер) тогда выберем время: с 11:00 по 18:00 по мск, в какое время будет удобнее?"
        return "на это время может не быть слота( подскажи, пожалуйста, время с 11:00 по 18:00"
    if stage == "room_available_check":
        if profile.get("interview_interest"):
            return "супер) тогда быстро уточню пару рабочих вопросиков. получится организовать место, где во время эфира тебе никто не помешает?"
        return "поняла) а получится организовать место, где во время эфира тебе никто не помешает?"
    if profile.get("interview_interest"):
        current_question = str(state.get("pending_question_text") or state.get("current_question") or "").strip()
        if stage == "interview_day_check" and current_question:
            return f"супер) тогда уточню день: {current_question}"
        if current_question:
            return f"супер) тогда быстро уточню пару рабочих вопросиков. {current_question}"
    return None


def non_null_facts(semantic: SemanticResult) -> dict[str, Any]:
    return {key: value for key, value in semantic.facts.model_dump().items() if value is not None}


def join_text(first: str | None, second: str | None) -> str:
    parts = [part.strip() for part in (first, second) if part and part.strip()]
    return "\n\n".join(parts)


def reply_to_json(result: ReplyResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
