from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.state import FunnelGraphState, latest_inbound_text


PROMPT_PATH = PROJECT_ROOT / "prompts" / "semantic_analyzer.md"


MessageType = Literal[
    "empty",
    "stage_answer",
    "interrupt_question",
    "objection",
    "mixed",
    "partial_answer",
    "soft_refusal",
    "hard_refusal",
    "do_not_contact",
    "unclear",
    "pause",
]


class SemanticFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interest_confirmed: bool | None = None
    interest_status: str | None = None
    age: int | None = None
    age_confirmed: bool | None = None
    salary_schedule_interest: bool | None = None
    questions_resolved: bool | None = None
    profile_info: str | None = None
    work_or_study: str | None = None
    hobbies: str | None = None
    room_available: bool | None = None
    room_note: str | None = None
    equipment_available: bool | None = None
    phone_model: str | None = None
    interview_interest: bool | None = None
    candidate_name: str | None = None
    phone_number: str | None = None
    interview_day_confirmed: bool | None = None
    interview_day: str | None = None
    interview_time: str | None = None
    custom_interview_datetime: str | None = None
    qualification_status: str | None = None


class SemanticResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_type: MessageType = "unclear"
    summary: str = ""
    current_goal_satisfied: bool = False
    has_unresolved_interrupt: bool = False
    interrupt_type: Literal["none", "question", "objection", "refusal", "unclear", "pause"] = "none"
    interrupt_topic: str | None = None
    interrupt_text: str | None = None
    facts: SemanticFacts = Field(default_factory=SemanticFacts)
    retrieval_query: str = ""
    retrieval_topics: list[str] = Field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.0


class SemanticAnalyzer:
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

    async def run(self, state: FunnelGraphState) -> SemanticResult:
        text = latest_inbound_text(state)
        if not text.strip():
            return SemanticResult(message_type="empty", summary="start or action turn", confidence=1.0)
        if self.use_llm and self.adapter.has_api_key("dialogue_brain"):
            try:
                payload = await self.adapter.complete_json(
                    component="dialogue_brain",
                    system_prompt=self._render_prompt(state),
                    user_payload=self._user_payload(state),
                    response_model=SemanticResult,
                )
                parsed = SemanticResult.model_validate(payload)
                return merge_deterministic_facts(parsed, deterministic_semantic(state))
            except BrainLLMError as exc:
                if not (self.fallback_on_llm_error or is_recoverable_llm_format_error(exc)):
                    raise
            except (ValidationError, ValueError, TypeError):
                # The external provider was called, but its JSON shape was not
                # usable. Keep the graph moving with the deterministic analyzer.
                pass
        return deterministic_semantic(state)

    def _render_prompt(self, state: FunnelGraphState) -> str:
        return self.prompt_path.read_text(encoding="utf-8")

    def _user_payload(self, state: FunnelGraphState) -> dict[str, Any]:
        return {
            "candidate_state": state.get("candidate_profile") or {},
            "current_state": state.get("stage"),
            "current_goal": state.get("current_goal"),
            "pending_question": state.get("pending_question_text") or state.get("current_question"),
            "resume_state": state.get("resume_state"),
            "recent_messages": state.get("recent_messages") or [],
            "incoming_message": state.get("incoming_message") or latest_inbound_text(state),
        }


def merge_deterministic_facts(llm: SemanticResult, fallback: SemanticResult) -> SemanticResult:
    llm_data = llm.model_dump()
    facts = llm.facts.model_dump()
    fallback_facts = fallback.facts.model_dump()
    for key, value in fallback_facts.items():
        if value is not None and facts.get(key) in (None, "", []):
            facts[key] = value
    llm_data["facts"] = facts
    if not llm_data.get("retrieval_query"):
        llm_data["retrieval_query"] = fallback.retrieval_query
    llm_data["retrieval_topics"] = merge_retrieval_topics(llm, fallback)
    if fallback_should_override_partial(llm, fallback):
        llm_data["message_type"] = "partial_answer"
        llm_data["current_goal_satisfied"] = False
        llm_data["has_unresolved_interrupt"] = False
        llm_data["interrupt_type"] = "none"
        llm_data["interrupt_topic"] = None
        llm_data["interrupt_text"] = None
        llm_data["evidence"] = fallback.evidence or llm.evidence
    elif fallback.has_unresolved_interrupt and not fallback.current_goal_satisfied:
        llm_data["message_type"] = fallback.message_type
        llm_data["current_goal_satisfied"] = False
        llm_data["has_unresolved_interrupt"] = True
        llm_data["interrupt_type"] = fallback.interrupt_type
        llm_data["interrupt_topic"] = fallback.interrupt_topic
        llm_data["interrupt_text"] = fallback.interrupt_text
        llm_data["evidence"] = fallback.evidence or llm.evidence
    elif fallback.message_type == "stage_answer" and fallback.current_goal_satisfied:
        # Deterministic extraction is deliberately narrow. If it sees a required
        # field answer, do not let a looser LLM classification stall the funnel.
        llm_data["message_type"] = "stage_answer"
        llm_data["current_goal_satisfied"] = True
        llm_data["has_unresolved_interrupt"] = False
        llm_data["interrupt_type"] = "none"
        llm_data["interrupt_topic"] = None
        llm_data["interrupt_text"] = None
        llm_data["evidence"] = fallback.evidence or llm.evidence
    return SemanticResult.model_validate(llm_data)


def fallback_should_override_partial(llm: SemanticResult, fallback: SemanticResult) -> bool:
    if fallback.message_type != "partial_answer":
        return False
    text = fallback.retrieval_query or fallback.interrupt_text or ""
    normalized = normalize_text(text)
    if is_question_like(text, normalized, infer_topics(normalized)):
        return False
    fallback_facts = fallback.facts.model_dump()
    if any(value is not None for value in fallback_facts.values()):
        return True
    return llm.has_unresolved_interrupt and llm.interrupt_topic in {"equipment", "contact"}


def is_recoverable_llm_format_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in ("json", "schema", "validation", "enum", "literal"))


def merge_retrieval_topics(llm: SemanticResult, fallback: SemanticResult) -> list[str]:
    topics: list[str] = []
    for source in (
        fallback.retrieval_topics,
        [fallback.interrupt_topic] if fallback.interrupt_topic else [],
        llm.retrieval_topics,
        [llm.interrupt_topic] if llm.interrupt_topic else [],
    ):
        for topic in source:
            if topic and topic not in topics:
                topics.append(str(topic))
    return topics[:6]


def deterministic_semantic(state: FunnelGraphState) -> SemanticResult:
    stage = str(state.get("stage") or "interest_check")
    text = repair_mojibake(latest_inbound_text(state).strip())
    normalized = normalize_text(text)
    facts: dict[str, Any] = {}
    topics = infer_topics(normalized)
    has_question = is_question_like(text, normalized, topics)
    has_objection = is_objection_like(normalized, topics)
    agreement = is_agreement(normalized)

    if is_do_not_contact(normalized):
        return SemanticResult(
            message_type="do_not_contact",
            summary="candidate asked not to contact",
            has_unresolved_interrupt=True,
            interrupt_type="refusal",
            interrupt_topic="do_not_contact",
            interrupt_text=text,
            retrieval_query=text,
            retrieval_topics=["do_not_contact"],
            confidence=0.98,
        )
    if is_hard_refusal(normalized, stage):
        return SemanticResult(
            message_type="hard_refusal",
            summary="candidate refused",
            has_unresolved_interrupt=True,
            interrupt_type="refusal",
            interrupt_topic="refusal",
            interrupt_text=text,
            retrieval_query=text,
            retrieval_topics=["refusal"],
            confidence=0.92,
        )
    if is_pause(normalized):
        return SemanticResult(
            message_type="pause",
            summary="candidate asked to continue later",
            has_unresolved_interrupt=True,
            interrupt_type="pause",
            interrupt_topic="no_time",
            interrupt_text=text,
            retrieval_query=text,
            retrieval_topics=["no_time"],
            confidence=0.9,
        )

    if stage == "interest_check":
        if agreement and not has_question and not has_objection:
            facts["interest_confirmed"] = True
            facts["interest_status"] = "interested"
            return stage_answer(text, facts, "interest confirmed")
    elif stage == "age_check":
        age = extract_age(normalized)
        if age is not None:
            facts["age"] = age
            facts["age_confirmed"] = age >= 18
            facts["qualification_status"] = "age_ok" if age >= 18 else "underage"
            return stage_answer(text, facts, "age provided") if age >= 18 else hard_refusal_result(text, "underage")
    elif stage == "salary_schedule_offer":
        if agreement and not has_question and not has_objection:
            facts["salary_schedule_interest"] = True
            return stage_answer(text, facts, "salary/schedule interest confirmed")
    elif stage == "post_equipment_questions_check":
        if questions_are_resolved(normalized):
            facts["questions_resolved"] = True
            return stage_answer(text, facts, "questions resolved")
        if topics:
            return SemanticResult(
                message_type="interrupt_question",
                summary="candidate asks a question after info materials",
                current_goal_satisfied=False,
                has_unresolved_interrupt=True,
                interrupt_type="question",
                interrupt_topic=topics[0],
                interrupt_text=text,
                facts=SemanticFacts.model_validate(facts),
                retrieval_query=text,
                retrieval_topics=topics,
                evidence="question topic at ASK_ANY_QUESTIONS stage",
                confidence=0.82,
            )
    elif stage == "profile_theme_check":
        if looks_like_profile_info(normalized):
            facts["profile_info"] = text
            facts["work_or_study"] = extract_work_or_study(text)
            facts["hobbies"] = extract_hobbies(text)
            return stage_answer(text, facts, "profile info provided")
    elif stage == "room_available_check":
        room = extract_room_available(normalized)
        if room is not None:
            facts["room_available"] = room
            facts["room_note"] = text
            return stage_answer(text, facts, "room availability answered") if room else partial_or_objection(text, facts, "room_not_available")
    elif stage == "equipment_phone_check":
        if mentions_equipment(normalized) and not has_question:
            facts["equipment_available"] = True
        phone_model = extract_phone_model(text)
        if phone_model:
            facts["phone_model"] = phone_model
            return stage_answer(text, facts, "phone model provided")
        if facts.get("equipment_available"):
            return partial_answer(text, facts, ["equipment"])
    elif stage == "interview_offer":
        if agreement and not has_question and not has_objection:
            facts["interview_interest"] = True
            return stage_answer(text, facts, "interview accepted")
    elif stage == "contact_collection":
        phone = extract_phone_number(text)
        name = extract_candidate_name(text)
        if phone:
            facts["phone_number"] = phone
        if name:
            facts["candidate_name"] = name
        if phone or name:
            return stage_answer(text, facts, "contact data provided") if phone and name else partial_answer(text, facts, ["contact"])
    elif stage == "interview_day_check":
        if is_negative(normalized):
            day = extract_interview_day(text) or text
            facts["interview_day_confirmed"] = False
            facts["interview_day"] = day
            return stage_answer(text, facts, "tomorrow is not convenient")
        if agreement:
            facts["interview_day_confirmed"] = True
            facts["interview_day"] = "завтра"
            return stage_answer(text, facts, "tomorrow confirmed")
        day = extract_interview_day(text)
        if day:
            facts["interview_day_confirmed"] = False
            facts["interview_day"] = day
            return stage_answer(text, facts, "custom interview day provided")
    elif stage == "interview_time_check":
        time = extract_interview_time(text)
        if time:
            facts["interview_time"] = time
            if time_in_range(time, 11, 18):
                return stage_answer(text, facts, "interview time selected")
            return partial_or_objection(text, facts, "time_out_of_range")
    elif stage == "interview_custom_time":
        day = extract_interview_day(text)
        time = extract_interview_time(text)
        if day and time:
            facts["custom_interview_datetime"] = f"{day} {time}"
            facts["interview_day"] = day
            facts["interview_time"] = time
            return stage_answer(text, facts, "custom interview date/time selected")
        if day or time:
            facts["interview_day"] = day
            facts["interview_time"] = time
            return partial_answer(text, facts, ["custom_interview_time"])

    if has_question or has_objection:
        message_type: MessageType = "mixed" if agreement or facts else ("objection" if has_objection else "interrupt_question")
        return SemanticResult(
            message_type=message_type,
            summary="candidate has an interrupt",
            current_goal_satisfied=False,
            has_unresolved_interrupt=True,
            interrupt_type="objection" if has_objection else "question",
            interrupt_topic=topics[0] if topics else "unknown",
            interrupt_text=text,
            facts=SemanticFacts.model_validate(facts),
            retrieval_query=text,
            retrieval_topics=topics,
            evidence="question/objection must be answered before moving funnel",
            confidence=0.8,
        )

    return SemanticResult(
        message_type="unclear",
        summary="message is not enough to satisfy current goal",
        current_goal_satisfied=False,
        has_unresolved_interrupt=True,
        interrupt_type="unclear",
        interrupt_topic="unclear",
        interrupt_text=text,
        retrieval_query=text,
        retrieval_topics=topics,
        evidence="no reliable answer to active question",
        confidence=0.55,
    )


def stage_answer(text: str, facts: dict[str, Any], evidence: str) -> SemanticResult:
    return SemanticResult(
        message_type="stage_answer",
        summary=evidence,
        current_goal_satisfied=True,
        has_unresolved_interrupt=False,
        facts=SemanticFacts.model_validate(facts),
        retrieval_query=text,
        retrieval_topics=infer_topics(normalize_text(text)),
        evidence=evidence,
        confidence=0.86,
    )


def partial_answer(text: str, facts: dict[str, Any], topics: list[str]) -> SemanticResult:
    return SemanticResult(
        message_type="partial_answer",
        summary="candidate provided partial data",
        current_goal_satisfied=False,
        has_unresolved_interrupt=False,
        facts=SemanticFacts.model_validate(facts),
        retrieval_query=text,
        retrieval_topics=topics,
        evidence="partial answer, missing required fields",
        confidence=0.82,
    )


def partial_or_objection(text: str, facts: dict[str, Any], topic: str) -> SemanticResult:
    return SemanticResult(
        message_type="objection",
        summary=topic,
        current_goal_satisfied=False,
        has_unresolved_interrupt=True,
        interrupt_type="objection",
        interrupt_topic=topic,
        interrupt_text=text,
        facts=SemanticFacts.model_validate(facts),
        retrieval_query=text,
        retrieval_topics=[topic],
        evidence=topic,
        confidence=0.8,
    )


def hard_refusal_result(text: str, topic: str) -> SemanticResult:
    return SemanticResult(
        message_type="hard_refusal",
        summary=topic,
        has_unresolved_interrupt=True,
        interrupt_type="refusal",
        interrupt_topic=topic,
        interrupt_text=text,
        retrieval_query=text,
        retrieval_topics=[topic],
        confidence=0.9,
    )


def normalize_text(text: str) -> str:
    lowered = repair_mojibake(text).strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", lowered)


def repair_mojibake(text: str) -> str:
    if "Р" not in text and "С" not in text:
        return text
    try:
        repaired = text.encode("cp1251").decode("utf-8")
    except UnicodeError:
        return text
    # Keep the repaired version only when it plausibly restored Cyrillic text.
    return repaired if any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in repaired) else text


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def is_agreement(text: str) -> bool:
    return bool(
        re.search(
            r"(^|[\s,!.?])(да|давай|хорошо|ок|окей|слушаю|расскажи|интересно|можем|супер|готова|согласна)([\s,!.?]|$)",
            text,
        )
    ) or "попроб" in text or "соглас" in text


def is_negative(text: str) -> bool:
    return bool(re.search(r"(^|[\s,!.?])(нет|неа|не удобно|неудобно|не могу)([\s,!.?]|$)", text))


def is_do_not_contact(text: str) -> bool:
    return contains_any(text, ("не пиши", "не пишите", "не беспокой", "удали", "отпиши", "не надо писать"))


def is_hard_refusal(text: str, stage: str) -> bool:
    if "вопросов нет" in text or "пока вопросов" in text:
        return False
    strong = ("не интересно", "неинтересно", "не актуально", "отказываюсь", "не хочу", "не подходит")
    if contains_any(text, strong):
        return True
    return text in {"нет", "нет спасибо", "неа"} and stage in {"interest_check", "salary_schedule_offer", "interview_offer"}


def is_pause(text: str) -> bool:
    return contains_any(text, ("через час", "позже", "потом", "занята", "занят", "не сейчас", "попозже"))


def infer_topics(text: str) -> list[str]:
    topic_markers = [
        ("contact_source", ("откуда", "контакт", "номер", "нашли", "нашла", "нашел", "нашёл", "аккаунт")),
        ("why_selected", ("почему", "заинтересовала", "выбрали", "подошла")),
        ("job_description", ("что за работа", "обязан", "что делать", "суть", "предложение")),
        ("nudity_onlyfans", ("огол", "голая", "onlyfans", "онлифанс", "интим", "эрот")),
        ("income", ("доход", "зарплата", "зп", "платят", "сколько")),
        ("schedule", ("график", "смен", "когда работать")),
        ("equipment", ("оборуд", "камера", "свет", "микрофон")),
        ("phone_requirements", ("телефон", "айфон", "iphone", "samsung", "самсунг", "модель")),
        ("payment_process", ("оплата", "выплаты", "деньги", "карта")),
        ("contract_gph", ("договор", "гпх", "официально", "документы")),
        ("company_info", ("компания", "кто вы", "profitcast", "профит")),
        ("interview_process", ("собесед", "интервью", "созвон", "зум", "zoom")),
        ("room", ("комната", "место", "помешает", "одна")),
        ("trust_concern", ("довер", "сомнев", "странно", "опас")),
        ("suspicious_or_scam", ("скам", "мошен", "развод", "обман", "подозр")),
        ("no_experience", ("нет опыта", "не умею", "никогда", "без опыта", "стесня", "не работала", "не работал", "не работаю на", "мой уровень")),
        ("no_time", ("нет времени", "занята", "позже", "не сейчас")),
    ]
    topics = [topic for topic, markers in topic_markers if contains_any(text, markers)]
    return topics[:5]


def is_question_like(raw: str, text: str, topics: list[str]) -> bool:
    if "?" in raw:
        return True
    return bool(topics) and contains_any(text, ("что", "как", "сколько", "откуда", "почему", "зачем", "какой", "какая", "можно"))


def is_objection_like(text: str, topics: list[str]) -> bool:
    objection_topics = {"trust_concern", "suspicious_or_scam", "nudity_onlyfans", "no_experience", "no_time"}
    if any(topic in objection_topics for topic in topics):
        return True
    return contains_any(text, ("но", "боюсь", "стесняюсь", "не уверена", "сомневаюсь", "не понятно", "непонятно"))


def extract_age(text: str) -> int | None:
    for match in re.finditer(r"\b(1[4-9]|[2-6]\d)\b", text):
        return int(match.group(1))
    if text in {"18+", "есть 18", "совершеннолетняя"}:
        return 18
    return None


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
            "всё понятно",
            "понятно",
        ),
    )


def looks_like_profile_info(text: str) -> bool:
    if len(text) < 8:
        return False
    work_or_study_markers = ("учусь", "студент", "студентка", "универ", "колледж", "школ", "институт", "работаю", "подрабатываю")
    hobby_context_markers = ("люблю", "хобби", "занимаюсь", "свободное", "увлекаюсь", "интересуюсь")
    return contains_any(text, work_or_study_markers) or contains_any(text, hobby_context_markers)


def extract_work_or_study(text: str) -> str | None:
    normalized = normalize_text(text)
    parts = []
    for marker in ("учусь", "студент", "студентка", "универ", "колледж", "школ", "институт", "практика"):
        if marker in normalized:
            parts.append(marker)
    if contains_any(normalized, ("не работаю", "не работала", "не работал")):
        parts.append("не работаю")
    elif contains_any(normalized, ("работаю", "подрабатываю", "работа 2/2")):
        parts.append("работаю")
    return ", ".join(dict.fromkeys(parts)) or None


def extract_hobbies(text: str) -> str | None:
    normalized = normalize_text(text)
    markers = ("люблю", "хобби", "занимаюсь", "рис", "макияж", "краш", "гуля", "тик ток", "tiktok", "сериал")
    return text.strip() if contains_any(normalized, markers) else None


def extract_room_available(text: str) -> bool | None:
    if contains_any(text, ("да", "есть", "найду", "смогу", "можно")) and not is_negative(text):
        return True
    if contains_any(text, ("нет", "неа", "не могу", "негде", "нет комнаты")):
        return False
    return None


def mentions_equipment(text: str) -> bool:
    return contains_any(text, ("оборудован", "камера", "микрофон", "свет", "стрим"))


def extract_phone_model(text: str) -> str | None:
    normalized = normalize_text(text)
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


def extract_phone_number(text: str) -> str | None:
    digits = re.sub(r"\D+", "", text)
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return "7" + digits[1:] if digits[0] == "8" else digits
    if len(digits) == 10:
        return "7" + digits
    return None


def extract_candidate_name(text: str) -> str | None:
    without_phone = re.sub(r"\+?\d[\d\s().-]{8,}\d", " ", text).strip()
    words = re.findall(r"\b[А-ЯЁA-Z][а-яёa-z]{1,24}\b", without_phone)
    if words:
        ignored = {"Привет", "Да", "Нет", "Хорошо"}
        for word in words:
            if word not in ignored:
                return word
    lower_words = re.findall(r"\b[а-яёa-z]{2,24}\b", without_phone, flags=re.IGNORECASE)
    if len(lower_words) == 1 and not infer_topics(normalize_text(lower_words[0])):
        return lower_words[0].capitalize()
    return None


def extract_interview_day(text: str) -> str | None:
    normalized = normalize_text(text)
    for marker in ("послезавтра", "сегодня", "завтра"):
        if marker in normalized:
            return marker
    match = re.search(r"\b(\d{1,2}[./-]\d{1,2})(?:[./-]\d{2,4})?\b", normalized)
    if match:
        return match.group(0)
    match = re.search(r"\b([12]?\d|3[01])\b", normalized)
    if match and contains_any(normalized, ("числ", "мая", "июн", "июл", "авг", "сент", "окт", "нояб", "дек")):
        return match.group(0)
    return None


def extract_interview_time(text: str) -> str | None:
    match = re.search(r"\b([01]?\d|2[0-3])(?:[:. ]([0-5]\d))?\b", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    return f"{hour:02d}:{minute:02d}"


def time_in_range(value: str, start: int, end: int) -> bool:
    hour = int(value.split(":", 1)[0])
    return start <= hour <= end


def semantic_to_json(result: SemanticResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
