from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.funnel_policy import (
    CANDIDATE_PROFILE_FIELDS,
    get_stage_policy,
    next_stage_if_requirement_met,
)
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.state import FunnelGraphState, latest_inbound_text


PROMPT_PATH = PROJECT_ROOT / "prompts" / "dialogue_orchestrator.md"


class DetectedQuestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    topic: str = ""
    text: str = ""


class DetectedObjection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = ""
    text: str = ""


class Understanding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    candidate_goal: str = ""
    dialogue_acts: list[str] = Field(default_factory=list)
    is_answer_to_current_stage_goal: bool = False
    stage_goal_completed: bool = False
    questions_detected: list[DetectedQuestion] = Field(default_factory=list)
    objections_detected: list[DetectedObjection] = Field(default_factory=list)
    implicit_signals: list[str] = Field(default_factory=list)
    interest_level: Literal["none", "low", "medium", "high", "unclear"] = "unclear"
    confidence: float = 0.0


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interest_confirmed: bool | None = None
    age_confirmed: bool | None = None
    salary_schedule_interest: bool | None = None
    phone_model: str | None = None
    equipment_available: bool | None = None
    questions_resolved: bool | None = None
    wants_to_try: bool | None = None
    profile_info: str | None = None
    work_or_study: str | None = None
    hobbies: str | None = None
    interview_interest: bool | None = None
    candidate_name: str | None = None
    phone_number: str | None = None
    interview_day_confirmed: bool | None = None
    interview_day: str | None = None
    interview_time: str | None = None


class TransitionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    current_stage: str = ""
    target_stage: str = ""
    transition_reason: str = ""
    stage_completed: bool = False


class NextStep(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal[
        "answer_and_continue",
        "ask_current_question",
        "ask_next_question",
        "send_voice_pack",
        "send_company_intro",
        "close_lost",
        "do_not_contact",
        "handoff_to_human",
        "finish_interview_booking",
    ] = "ask_current_question"
    question_to_ask: str | None = None


class OrchestratorOutgoingMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "voice_pack"] = "text"
    text: str | None = None
    voice_pack_id: str | None = None


class ReplyDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    send: bool = True
    text: str | None = ""


class HandoffDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    needed: bool = False
    reason: str | None = None


class DialogueOrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    understanding: Understanding = Field(default_factory=Understanding)
    state_patch: StatePatch = Field(default_factory=StatePatch)
    transition: TransitionDecision = Field(default_factory=TransitionDecision)
    next_step: NextStep = Field(default_factory=NextStep)
    outgoing_messages: list[OrchestratorOutgoingMessage] = Field(default_factory=list)
    reply: ReplyDecision = Field(default_factory=ReplyDecision)
    handoff: HandoffDecision = Field(default_factory=HandoffDecision)


class DialogueOrchestrator:
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

    async def run(self, state: FunnelGraphState) -> DialogueOrchestratorResult:
        if not normalize_text(latest_inbound_text(state)):
            return deterministic_orchestrator(state)
        if self.use_llm and not self.adapter.has_api_key("dialogue_brain") and not self.fallback_on_llm_error:
            raise BrainLLMError("Missing API key for dialogue_brain")
        if self.use_llm and self.adapter.has_api_key("dialogue_brain"):
            try:
                payload = await self.adapter.complete_json(
                    component="dialogue_brain",
                    system_prompt=self._render_prompt(state),
                    user_payload=self._user_payload(state),
                    response_model=DialogueOrchestratorResult,
                )
                return DialogueOrchestratorResult.model_validate(payload)
            except BrainLLMError:
                if not self.fallback_on_llm_error:
                    raise
                return deterministic_orchestrator(state)
            except (ValueError, TypeError):
                raise
        return deterministic_orchestrator(state)

    def _render_prompt(self, state: FunnelGraphState) -> str:
        template = self.prompt_path.read_text(encoding="utf-8")
        replacements = {
            "{candidate_state}": json.dumps(state.get("candidate_profile") or {}, ensure_ascii=False, default=str),
            "{current_stage}": str(state.get("stage") or ""),
            "{current_goal}": str(state.get("current_goal") or ""),
            "{current_question}": str(state.get("current_question") or ""),
            "{next_stage_if_completed}": str(state.get("next_stage_if_completed") or ""),
            "{recent_messages}": json.dumps(state.get("recent_messages") or [], ensure_ascii=False, default=str),
            "{incoming_message}": str(state.get("incoming_message") or latest_inbound_text(state)),
            "{faq_context}": json.dumps(state.get("faq_context") or [], ensure_ascii=False, default=str),
            "{objection_context}": json.dumps(state.get("objection_context") or [], ensure_ascii=False, default=str),
            "{voice_packs}": json.dumps(state.get("voice_packs") or {}, ensure_ascii=False, default=str),
            "{response_rules}": json.dumps(state.get("response_rules") or {}, ensure_ascii=False, default=str),
        }
        for key, value in replacements.items():
            template = template.replace(key, value)
        return template

    def _user_payload(self, state: FunnelGraphState) -> dict[str, Any]:
        return {
            "candidate_state": state.get("candidate_profile") or {},
            "current_stage": state.get("stage"),
            "current_goal": state.get("current_goal"),
            "current_question": state.get("current_question"),
            "next_stage_if_completed": state.get("next_stage_if_completed"),
            "recent_messages": state.get("recent_messages") or [],
            "incoming_message": state.get("incoming_message") or latest_inbound_text(state),
            "faq_context": state.get("faq_context") or [],
            "objection_context": state.get("objection_context") or [],
            "voice_packs": state.get("voice_packs") or {},
            "response_rules": state.get("response_rules") or {},
        }


def deterministic_orchestrator(state: FunnelGraphState) -> DialogueOrchestratorResult:
    stage = str(state.get("stage") or "interest_check")
    text = latest_inbound_text(state)
    normalized = normalize_text(text)
    policy = get_stage_policy(stage)
    profile = dict(state.get("candidate_profile") or {})
    faq_context = list(state.get("faq_context") or [])
    objection_context = list(state.get("objection_context") or [])

    if not normalized:
        first_touch = str((state.get("retrieved_knowledge") or {}).get("first_touch_message") or "")
        return result(
            stage=stage,
            target_stage=stage,
            action="ask_current_question",
            reply_text=first_touch or policy.current_question or "",
            outgoing_text=first_touch or policy.current_question,
            summary="start dialog",
        )

    if is_do_not_contact(normalized):
        return result(
            stage=stage,
            target_stage="do_not_contact",
            action="do_not_contact",
            reply_send=False,
            summary="candidate asked not to contact",
            confidence=0.98,
        )

    if is_later(normalized):
        return result(
            stage=stage,
            target_stage=stage,
            action="answer_and_continue",
            reply_text="Хорошо, буду ждать.",
            outgoing_text="Хорошо, буду ждать.",
            summary="candidate asked to continue later",
            confidence=0.9,
        )

    if is_explicit_refusal(normalized, stage=stage):
        return result(
            stage=stage,
            target_stage="lost",
            action="close_lost",
            reply_text="Поняла, не буду отвлекать. Хорошего дня!",
            outgoing_text="Поняла, не буду отвлекать. Хорошего дня!",
            summary="candidate refused",
            confidence=0.95,
        )

    questions = detected_questions(text, faq_context)
    objections = detected_objections(text, objection_context)
    answer_text = knowledge_answer(faq_context, objection_context)
    patch: dict[str, Any] = {}
    stage_completed = False
    target_stage = stage
    action = "ask_current_question"
    outgoing_text: str | None = None

    if stage == "interest_check":
        if is_agreement(normalized):
            patch["interest_confirmed"] = True
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "age_check":
        age = extract_age(normalized)
        if age is not None and age >= 18:
            patch["age_confirmed"] = True
            stage_completed = True
        elif age is not None and age < 18:
            return result(
                stage=stage,
                target_stage="lost",
                action="close_lost",
                reply_text="Поняла. К сожалению, мы можем рассматривать только совершеннолетних кандидатов.",
                outgoing_text="Поняла. К сожалению, мы можем рассматривать только совершеннолетних кандидатов.",
                summary="candidate is under 18",
                confidence=0.95,
            )
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "salary_schedule_offer":
        if is_agreement(normalized):
            patch["salary_schedule_interest"] = True
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "equipment_phone_check":
        if mentions_equipment(normalized):
            patch["equipment_available"] = True
        phone_model = extract_phone_model(text)
        if phone_model:
            patch["phone_model"] = phone_model
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "post_equipment_questions_check":
        if questions_are_resolved(normalized):
            patch["questions_resolved"] = True
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "try_interest_check":
        if is_agreement(normalized):
            patch["wants_to_try"] = True
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "profile_theme_check":
        if looks_like_profile_info(normalized):
            patch["profile_info"] = text.strip()
            patch["work_or_study"] = extract_work_or_study(text)
            patch["hobbies"] = extract_hobbies(text)
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "interview_offer":
        if is_agreement(normalized):
            patch["interview_interest"] = True
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "contact_collection":
        phone_number = extract_phone_number(text)
        name = extract_candidate_name(text)
        if phone_number:
            patch["phone_number"] = phone_number
        if name:
            patch["candidate_name"] = name
        merged = {**profile, **{key: value for key, value in patch.items() if value is not None}}
        stage_completed = bool(merged.get("candidate_name")) and bool(merged.get("phone_number"))
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "interview_day_check":
        if is_agreement(normalized):
            patch["interview_day_confirmed"] = True
            patch["interview_day"] = "завтра"
            stage_completed = True
        else:
            day = extract_interview_day(text)
            if day:
                patch["interview_day"] = day
                stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    elif stage == "interview_time_check":
        interview_time = extract_interview_time(text)
        if interview_time:
            patch["interview_time"] = interview_time
            stage_completed = True
        target_stage = next_stage_after_patch(stage, profile, patch)

    questions = promote_faq_context_to_questions(
        stage=stage,
        text=text,
        faq_context=faq_context,
        existing_questions=questions,
        stage_completed=stage_completed,
        patch=patch,
    )

    if answer_text and (questions or objections) and not stage_completed:
        outgoing_text = join_text(answer_text, policy.current_question)
        action = "answer_and_continue"
    elif answer_text and (questions or objections) and stage_completed:
        outgoing_text = answer_text
        action = "answer_and_continue"
    elif target_stage == stage and policy.current_question:
        outgoing_text = stage_specific_followup(stage, profile, patch) or policy.current_question
        action = "ask_current_question"
    elif target_stage != stage:
        action = "ask_next_question"

    return result(
        stage=stage,
        target_stage=target_stage,
        action=action,
        state_patch=patch,
        outgoing_text=outgoing_text,
        reply_text=outgoing_text or "",
        summary="deterministic funnel decision",
        questions=questions,
        objections=objections,
        stage_completed=stage_completed,
        confidence=0.88,
    )


def result(
    *,
    stage: str,
    target_stage: str,
    action: str,
    state_patch: dict[str, Any] | None = None,
    outgoing_text: str | None = None,
    reply_text: str = "",
    reply_send: bool = True,
    summary: str = "",
    questions: list[DetectedQuestion] | None = None,
    objections: list[DetectedObjection] | None = None,
    stage_completed: bool = False,
    confidence: float = 0.8,
) -> DialogueOrchestratorResult:
    outgoing = []
    if outgoing_text:
        outgoing.append(OrchestratorOutgoingMessage(type="text", text=outgoing_text))
    return DialogueOrchestratorResult(
        understanding=Understanding(
            summary=summary,
            dialogue_acts=[action],
            is_answer_to_current_stage_goal=stage_completed,
            stage_goal_completed=stage_completed,
            questions_detected=questions or [],
            objections_detected=objections or [],
            interest_level="medium" if stage_completed else "unclear",
            confidence=confidence,
        ),
        state_patch=StatePatch.model_validate(clean_state_patch(state_patch or {})),
        transition=TransitionDecision(
            current_stage=stage,
            target_stage=target_stage,
            transition_reason=summary,
            stage_completed=stage_completed,
        ),
        next_step=NextStep(action=action),  # type: ignore[arg-type]
        outgoing_messages=outgoing,
        reply=ReplyDecision(send=reply_send, text=reply_text or outgoing_text or ""),
    )


def clean_state_patch(patch: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in patch.items() if key in CANDIDATE_PROFILE_FIELDS}


def next_stage_after_patch(stage: str, profile: dict[str, Any], patch: dict[str, Any]) -> str:
    merged = {**profile, **{key: value for key, value in patch.items() if value is not None}}
    return next_stage_if_requirement_met(stage, merged)


def normalize_text(text: str) -> str:
    lowered = text.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", lowered)


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def is_agreement(text: str) -> bool:
    if "попроб" in text or "соглас" in text:
        return True
    return bool(
        re.search(
            r"(^|[\s,!.?])(да|давай|хорошо|ок|окей|слушаю|расскажи|интересно|можем)([\s,!.?]|$)",
            text,
        )
    )


def is_do_not_contact(text: str) -> bool:
    return contains_any(text, ("не пиши", "не пишите", "не беспокой", "удали", "отпиши", "не надо писать"))


def is_explicit_refusal(text: str, *, stage: str) -> bool:
    if "вопросов нет" in text or "пока вопросов" in text:
        return False
    if stage == "post_equipment_questions_check" and questions_are_resolved(text):
        return False
    strong = ("не интересно", "неинтересно", "не актуально", "отказываюсь", "не хочу", "не подходит")
    if contains_any(text, strong):
        return True
    return text in {"нет", "нет спасибо", "неа"} and stage in {
        "interest_check",
        "salary_schedule_offer",
        "try_interest_check",
        "interview_offer",
        "interview_day_check",
    }


def is_later(text: str) -> bool:
    return contains_any(text, ("через час", "позже", "потом", "занята", "занят", "не сейчас", "попозже"))


def extract_age(text: str) -> int | None:
    for match in re.finditer(r"\b(1[4-9]|[2-6]\d)\b", text):
        return int(match.group(1))
    if text in {"18+", "есть 18", "совершеннолетняя", "совершеннолетний"}:
        return 18
    return None


def mentions_equipment(text: str) -> bool:
    return contains_any(text, ("оборудован", "камера", "микрофон", "свет", "стрим"))


def extract_phone_model(text: str) -> str | None:
    normalized = normalize_text(text)
    if mentions_equipment(normalized) and not contains_any(
        normalized,
        ("iphone", "айфон", "samsung", "самсунг", "xiaomi", "redmi", "honor", "huawei", "pixel", "realme", "poco"),
    ):
        return None
    model_markers = (
        "iphone",
        "айфон",
        "samsung",
        "самсунг",
        "xiaomi",
        "redmi",
        "poco",
        "honor",
        "huawei",
        "oneplus",
        "pixel",
        "realme",
        "vivo",
        "oppo",
        "tecno",
        "infinix",
    )
    if not contains_any(normalized, model_markers):
        return None
    cleaned = re.sub(r"^(у меня|телефон|модель)\s+", "", text.strip(), flags=re.IGNORECASE)
    return cleaned.strip(" .,!?:;")[:80] or None


def questions_are_resolved(text: str) -> bool:
    return contains_any(
        text,
        (
            "пока нет",
            "вопросов нет",
            "нет вопросов",
            "по ходу разбер",
            "потом появ",
            "думаю появ",
            "все понятно",
            "понятно",
        ),
    )


def looks_like_profile_info(text: str) -> bool:
    return len(text) >= 10 and contains_any(
        text,
        ("учусь", "работаю", "работа", "универ", "колледж", "люблю", "хобби", "занимаюсь", "свободное"),
    )


def extract_work_or_study(text: str) -> str | None:
    parts = []
    for marker in ("учусь", "работаю", "работа", "универ", "колледж", "школ", "институт"):
        if marker in normalize_text(text):
            parts.append(marker)
    return ", ".join(dict.fromkeys(parts)) or None


def extract_hobbies(text: str) -> str | None:
    normalized = normalize_text(text)
    if "люблю" in normalized:
        return text.strip()
    if "хобби" in normalized or "занимаюсь" in normalized:
        return text.strip()
    return None


def extract_phone_number(text: str) -> str | None:
    digits = re.sub(r"\D+", "", text)
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return "7" + digits[1:] if digits[0] == "8" else digits
    if len(digits) == 10:
        return "7" + digits
    return None


def extract_candidate_name(text: str) -> str | None:
    without_phone = re.sub(r"\+?\d[\d\s().-]{8,}\d", " ", text).strip()
    match = re.search(r"\b[А-ЯЁA-Z][а-яёa-z]{1,24}\b", without_phone)
    if match:
        return match.group(0)
    words = re.findall(r"\b[а-яёa-z]{2,24}\b", without_phone, flags=re.IGNORECASE)
    if len(words) == 1 and not words[0].isdigit():
        return words[0].capitalize()
    return None


def extract_interview_day(text: str) -> str | None:
    normalized = normalize_text(text)
    for marker in ("послезавтра", "сегодня", "завтра"):
        if marker in normalized:
            return marker
    match = re.search(r"\b(\d{1,2}[./-]\d{1,2})(?:[./-]\d{2,4})?\b", normalized)
    return match.group(0) if match else None


def extract_interview_time(text: str) -> str | None:
    match = re.search(r"\b([01]?\d|2[0-3])(?:[:. ]([0-5]\d))?\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    return f"{hour:02d}:{minute:02d}"


def detected_questions(text: str, faq_context: list[dict[str, Any]]) -> list[DetectedQuestion]:
    normalized = normalize_text(text)
    has_marker = "?" in text or contains_any(
        normalized,
        ("что", "как", "сколько", "откуда", "почему", "зачем", "какой", "какая", "onlyfans", "онлифанс"),
    )
    if not has_marker:
        return []
    if faq_context:
        return [DetectedQuestion(topic=str(item.get("topic") or ""), text=text.strip()) for item in faq_context]
    return [DetectedQuestion(topic="unknown", text=text.strip())]


def promote_faq_context_to_questions(
    *,
    stage: str,
    text: str,
    faq_context: list[dict[str, Any]],
    existing_questions: list[DetectedQuestion],
    stage_completed: bool,
    patch: dict[str, Any],
) -> list[DetectedQuestion]:
    if existing_questions or not faq_context:
        return existing_questions
    topics = {str(item.get("topic") or "") for item in faq_context}
    if stage == "equipment_phone_check" and stage_completed and topics <= {"phone_requirements"}:
        return []
    if stage == "equipment_phone_check" and patch.get("equipment_available") and not patch.get("phone_model"):
        return []
    if stage == "profile_theme_check" and patch.get("profile_info"):
        return []
    if stage == "contact_collection" and (patch.get("candidate_name") or patch.get("phone_number")):
        return []
    interrupt_stages = {
        "interest_check",
        "age_check",
        "salary_schedule_offer",
        "equipment_phone_check",
        "post_equipment_questions_check",
        "try_interest_check",
        "profile_theme_check",
        "interview_offer",
        "interview_day_check",
        "interview_time_check",
    }
    if stage in interrupt_stages:
        return [DetectedQuestion(topic=str(item.get("topic") or ""), text=text.strip()) for item in faq_context]
    return existing_questions


def detected_objections(text: str, objection_context: list[dict[str, Any]]) -> list[DetectedObjection]:
    if objection_context:
        return [DetectedObjection(type=str(item.get("topic") or ""), text=text.strip()) for item in objection_context]
    return []


def knowledge_answer(faq_context: list[dict[str, Any]], objection_context: list[dict[str, Any]]) -> str:
    answers = []
    for item in [*faq_context, *objection_context]:
        answer = str(item.get("answer") or "").strip()
        if answer and answer not in answers:
            answers.append(answer)
    return " ".join(answers[:3])


def stage_specific_followup(stage: str, profile: dict[str, Any], patch: dict[str, Any]) -> str | None:
    merged = {**profile, **{key: value for key, value in patch.items() if value is not None}}
    if stage == "contact_collection":
        if merged.get("phone_number") and not merged.get("candidate_name"):
            return "Спасибо, номер получила. Напиши, пожалуйста, имя."
        if merged.get("candidate_name") and not merged.get("phone_number"):
            return "Спасибо. Теперь пришли, пожалуйста, номер телефона для записи."
    if stage == "equipment_phone_check" and merged.get("equipment_available") and not merged.get("phone_model"):
        return "Оборудование это плюс, но для старта всё равно уточню телефон. Какая у тебя модель?"
    return None


def join_text(first: str | None, second: str | None) -> str:
    parts = [part.strip() for part in (first, second) if part and part.strip()]
    return "\n\n".join(parts)
