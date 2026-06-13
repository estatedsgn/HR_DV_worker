from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.funnel_policy import stage_requirement_met
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.model_profiles import active_model_profile, is_complex_turn
from app.services.funnel_graph.persona import with_persona
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
    birthday: str | None = None
    birthday_18_at: str | None = None
    salary_schedule_interest: bool | None = None
    questions_resolved: bool | None = None
    profile_info: str | None = None
    work_or_study: str | None = None
    hobbies: str | None = None
    room_available: bool | None = None
    room_note: str | None = None
    equipment_available: bool | None = None
    phone_model: str | None = None
    phone_eligible: bool | None = None
    pc_webcam_available: bool | None = None
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

    @field_validator("interrupt_topic", mode="before")
    @classmethod
    def normalize_interrupt_topic_value(cls, value: Any) -> str | None:
        if value in (None, "", []):
            return None
        if isinstance(value, (list, tuple, set)):
            for item in value:
                normalized = normalize_topic_value(item)
                if normalized:
                    return normalized
            return None
        return normalize_topic_value(value)

    @field_validator("retrieval_topics", mode="before")
    @classmethod
    def normalize_retrieval_topics_value(cls, value: Any) -> list[str]:
        return normalize_topic_list(value)

    @model_validator(mode="after")
    def normalize_retrieval_fields(self) -> SemanticResult:
        self.retrieval_topics = compact_topics(self.retrieval_topics)
        if not any(is_specific_topic(topic) for topic in self.retrieval_topics):
            self.retrieval_topics = []
            self.retrieval_query = ""
        else:
            self.retrieval_query = normalize_retrieval_query(self.retrieval_query, self.retrieval_topics)
        return self


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
        self.last_run_metadata: dict[str, Any] = {}

    async def run(self, state: FunnelGraphState) -> SemanticResult:
        started = time.perf_counter()
        profile = active_model_profile(self.settings)
        text = latest_inbound_text(state)
        if not text.strip():
            self.last_run_metadata = {
                "profile": profile.name,
                "component": None,
                "model": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "fast_path": True,
                "status": "empty",
            }
            return SemanticResult(message_type="empty", summary="start or action turn", confidence=1.0)
        deterministic = deterministic_semantic(state)
        if profile.use_fast_path and semantic_fast_path_allowed(deterministic):
            self.last_run_metadata = {
                "profile": profile.name,
                "component": "deterministic",
                "model": None,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "fast_path": True,
                "status": "completed",
                "message_type": deterministic.message_type,
            }
            return deterministic
        if self.use_llm and self.adapter.has_api_key("semantic_analyzer"):
            component = "semantic_analyzer_complex" if profile.route_complex_to_max and is_complex_turn({**state, "semantic_result": deterministic.model_dump()}) else "semantic_analyzer"
            try:
                payload = await self.adapter.complete_json(
                    component=component,
                    system_prompt=self._render_prompt(state),
                    user_payload=self._user_payload(state),
                    response_model=SemanticResult,
                )
                parsed = SemanticResult.model_validate(payload)
                merged = merge_deterministic_facts(parsed, deterministic, state=state)
                self.last_run_metadata = {
                    "profile": profile.name,
                    "component": component,
                    "model": self.adapter.config_for(component, SemanticResult).model,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "fast_path": False,
                    "status": "completed",
                    "message_type": merged.message_type,
                }
                return merged
            except BrainLLMError as exc:
                fallback = await self._try_complex_fallback(state, deterministic, component, started, str(exc))
                if fallback is not None:
                    return fallback
                if not (self.fallback_on_llm_error or is_recoverable_llm_format_error(exc)):
                    raise
            except (ValidationError, ValueError, TypeError):
                fallback = await self._try_complex_fallback(state, deterministic, component, started, "invalid_json")
                if fallback is not None:
                    return fallback
                # The external provider was called, but its JSON shape was not
                # usable. Keep the graph moving with the deterministic analyzer.
                pass
        self.last_run_metadata = {
            "profile": profile.name,
            "component": "deterministic",
            "model": None,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "fast_path": True,
            "status": "fallback",
            "message_type": deterministic.message_type,
        }
        return deterministic

    async def _try_complex_fallback(
        self,
        state: FunnelGraphState,
        deterministic: SemanticResult,
        component: str,
        started: float,
        reason: str,
    ) -> SemanticResult | None:
        profile = active_model_profile(self.settings)
        fallback_component = "semantic_analyzer_complex"
        if not profile.route_complex_to_max or component == fallback_component or not self.adapter.has_api_key(fallback_component):
            return None
        try:
            payload = await self.adapter.complete_json(
                component=fallback_component,
                system_prompt=self._render_prompt(state),
                user_payload=self._user_payload(state),
                response_model=SemanticResult,
            )
            parsed = SemanticResult.model_validate(payload)
            merged = merge_deterministic_facts(parsed, deterministic, state=state)
            self.last_run_metadata = {
                "profile": profile.name,
                "component": fallback_component,
                "model": self.adapter.config_for(fallback_component, SemanticResult).model,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "fast_path": False,
                "status": "fallback_completed",
                "fallback_reason": reason,
                "message_type": merged.message_type,
            }
            return merged
        except Exception:
            return None

    def _render_prompt(self, state: FunnelGraphState) -> str:
        return with_persona(self.prompt_path.read_text(encoding="utf-8"))

    def _user_payload(self, state: FunnelGraphState) -> dict[str, Any]:
        return {
            "candidate_state": state.get("candidate_profile") or {},
            "current_state": state.get("stage"),
            "current_goal": state.get("current_goal"),
            "pending_question": state.get("pending_question_text") or state.get("current_question"),
            "resume_state": state.get("resume_state"),
            "recent_messages": state.get("recent_messages") or [],
            "message_batch": state.get("message_batch") or [],
            "incoming_message": state.get("incoming_message") or latest_inbound_text(state),
        }


def merge_deterministic_facts(
    llm: SemanticResult,
    fallback: SemanticResult,
    *,
    state: FunnelGraphState | None = None,
) -> SemanticResult:
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
    elif fallback_should_clear_generic_unclear(llm, fallback):
        llm_data["message_type"] = fallback.message_type
        llm_data["current_goal_satisfied"] = False
        llm_data["has_unresolved_interrupt"] = False
        llm_data["interrupt_type"] = "none"
        llm_data["interrupt_topic"] = None
        llm_data["interrupt_text"] = None
        llm_data["evidence"] = fallback.evidence or llm.evidence
    elif fallback_should_override_interrupt(llm, fallback):
        llm_data["message_type"] = fallback.message_type
        llm_data["current_goal_satisfied"] = False
        llm_data["has_unresolved_interrupt"] = True
        llm_data["interrupt_type"] = fallback.interrupt_type
        llm_data["interrupt_topic"] = stronger_interrupt_topic(llm.interrupt_topic, fallback.interrupt_topic)
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
    if state is not None:
        llm_data = enforce_current_goal_against_required_fields(llm_data, state)
    return normalize_semantic_result(SemanticResult.model_validate(llm_data))


def semantic_fast_path_allowed(result: SemanticResult) -> bool:
    if result.message_type == "stage_answer" and result.current_goal_satisfied and not result.has_unresolved_interrupt:
        return True
    if result.message_type == "partial_answer" and not result.has_unresolved_interrupt:
        return True
    return result.message_type in {"empty", "pause", "do_not_contact", "hard_refusal"} and result.confidence >= 0.85


def enforce_current_goal_against_required_fields(data: dict[str, Any], state: FunnelGraphState) -> dict[str, Any]:
    if not data.get("current_goal_satisfied") or data.get("has_unresolved_interrupt"):
        return data
    stage = str(state.get("stage") or "interest_check")
    facts = dict(data.get("facts") or {})
    merged_profile = {
        **dict(state.get("candidate_profile") or {}),
        **{key: value for key, value in facts.items() if value is not None},
    }
    if stage_requirement_met(stage, merged_profile):
        return data
    data["current_goal_satisfied"] = False
    if data.get("message_type") == "stage_answer":
        data["message_type"] = "unclear"
    data["evidence"] = data.get("evidence") or "active stage required fields are not satisfied"
    return data


def stronger_interrupt_topic(llm_topic: str | None, fallback_topic: str | None) -> str | None:
    if is_specific_topic(fallback_topic):
        return fallback_topic
    if is_specific_topic(llm_topic):
        return llm_topic
    return fallback_topic or llm_topic


def fallback_should_clear_generic_unclear(llm: SemanticResult, fallback: SemanticResult) -> bool:
    return (
        not fallback.has_unresolved_interrupt
        and fallback.message_type == "unclear"
        and llm_is_generic_unclear(llm)
    )


def fallback_should_override_interrupt(llm: SemanticResult, fallback: SemanticResult) -> bool:
    if not fallback.has_unresolved_interrupt or fallback.current_goal_satisfied:
        return False
    if is_generic_unclear_interrupt(fallback):
        return llm_is_generic_unclear(llm)
    return True


def llm_is_generic_unclear(result: SemanticResult) -> bool:
    if result.current_goal_satisfied:
        return False
    if result.message_type != "unclear" and result.interrupt_type not in {"none", "unclear"}:
        return False
    return not has_specific_topic(result)


def is_generic_unclear_interrupt(result: SemanticResult) -> bool:
    return not has_specific_topic(result) and (
        result.interrupt_type in {"none", "unclear"}
        or not is_specific_topic(result.interrupt_topic)
    )


def has_specific_topic(result: SemanticResult) -> bool:
    return is_specific_topic(result.interrupt_topic) or any(is_specific_topic(topic) for topic in result.retrieval_topics)


GENERIC_TOPICS = {"unknown", "unclear", "parse_error", "none", "null"}
OBJECTION_TOPICS = {
    "trust_concern",
    "suspicious_or_scam",
    "nudity_onlyfans",
    "no_experience",
    "new_sphere_uncertainty",
    "english_level",
    "no_time",
    "privacy_anonymity",
    "documents_privacy",
    "exit_policy",
    "soft_decline_income",
    "already_employed",
    # Из живых переписок 06-10 (см. docs/CHANGELOG_AGENT.md, пакет №2):
    "bot_suspicion",
    "already_in_industry",
    "legal_concern",
    "need_to_think",
    "wants_smalltalk_first",
}


def is_specific_topic(topic: str | None) -> bool:
    return bool(topic and str(topic).strip().lower() not in GENERIC_TOPICS)


# Broad "catch-all" topics whose trigger words (e.g. "стрим"/"трансляц" for the
# whole job) overlap with almost any domain question. They are valid answers only
# when nothing more specific was asked; when a specific topic co-occurs they are
# dropped so a pointed question ("на каких платформах?") gets a pointed answer
# instead of the entire job description blob.
BROAD_TOPICS = {"job_description"}


def demote_broad_topics(topics: list[str]) -> list[str]:
    """Drop broad catch-all topics when a more specific topic is present."""
    has_specific = any(
        topic not in BROAD_TOPICS and is_specific_topic(topic) for topic in topics
    )
    if not has_specific:
        return topics
    return [topic for topic in topics if topic not in BROAD_TOPICS]


def normalize_semantic_result(result: SemanticResult) -> SemanticResult:
    data = result.model_dump()
    data["retrieval_topics"] = compact_topics(data.get("retrieval_topics") or [])
    if result.has_unresolved_interrupt and result.interrupt_type == "unclear" and has_specific_topic(result):
        topic = first_specific_topic(result)
        data["interrupt_topic"] = topic
        data["interrupt_type"] = "objection" if topic in OBJECTION_TOPICS else "question"
        data["message_type"] = "objection" if topic in OBJECTION_TOPICS else "interrupt_question"
    if not data.get("has_unresolved_interrupt"):
        data["interrupt_type"] = "none"
        data["interrupt_topic"] = None
        data["interrupt_text"] = None
    return SemanticResult.model_validate(data)


def first_specific_topic(result: SemanticResult) -> str | None:
    if is_specific_topic(result.interrupt_topic):
        return result.interrupt_topic
    for topic in result.retrieval_topics:
        if is_specific_topic(topic):
            return topic
    return None


def compact_topics(topics: list[str]) -> list[str]:
    normalized = normalize_topic_list(topics)
    specific = [topic for topic in normalized if is_specific_topic(topic)]
    values = demote_broad_topics(specific or normalized)
    result: list[str] = []
    for topic in values:
        if topic not in result:
            result.append(topic)
    return result[:6]


def normalize_topic_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    raw_items: list[Any]
    if isinstance(value, str):
        raw_items = re.split(r"[,;/]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    topics: list[str] = []
    for item in raw_items:
        if isinstance(item, (list, tuple, set)):
            nested = normalize_topic_list(list(item))
            for nested_item in nested:
                if nested_item not in topics:
                    topics.append(nested_item)
            continue
        if isinstance(item, str) and re.search(r"[,;/]+", item):
            nested = normalize_topic_list(item)
            for nested_item in nested:
                if nested_item not in topics:
                    topics.append(nested_item)
            continue
        normalized = normalize_topic_value(item)
        if normalized and normalized not in topics:
            topics.append(normalized)
    return topics


def normalize_topic_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip().lower().replace("-", "_").replace(" ", "_") or None


def normalize_retrieval_query(query: str, topics: list[str]) -> str:
    if not topics:
        return ""
    text = repair_mojibake(str(query or "")).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return " ".join(text.split()[:12])


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
    if any(topic in {"equipment", "phone_requirements", "contact", "custom_interview_time"} for topic in fallback.retrieval_topics):
        return True
    return llm.has_unresolved_interrupt and llm.interrupt_topic in {"equipment", "phone_requirements", "contact"}


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
    return compact_topics(topics)


def deterministic_semantic(state: FunnelGraphState) -> SemanticResult:
    stage = str(state.get("stage") or "interest_check")
    text = repair_mojibake(latest_inbound_text(state).strip())
    normalized = normalize_text(text)
    facts: dict[str, Any] = {}
    topics = infer_topics(normalized)
    has_question = is_question_like(text, normalized, topics)
    has_objection = is_objection_like(normalized, topics)
    agreement = is_agreement(normalized)
    booking_intent = is_interview_booking_intent(normalized)

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
    # Несогласие/отказ ("не интересует", "у меня есть работа", "спасибо, но нет",
    # "нет" на гейте…): ПЕРВЫЙ раз отрабатываем возражение (деньги/совмещение) —
    # граф пометит soft_decline_rebutted. Если кандидат отказывается ПОВТОРНО, без
    # реального вопроса и новой конкретной темы — уводим в lost, не долбя питчем по
    # кругу. См. [[soft-decline-objection-once]].
    _meta = dict(state.get("metadata") or {})
    decline_topic = rebuttable_decline_topic(normalized)
    bare_gate_refusal = is_hard_refusal(normalized, stage)
    if (
        _meta.get("soft_decline_rebutted")
        and not has_question
        and (decline_topic is not None or bare_gate_refusal or is_negative(normalized))
        and not any(topic in OBJECTION_TOPICS for topic in topics)
    ):
        return hard_refusal_result(text, "decline_repeated")
    if not has_question and (decline_topic is not None or bare_gate_refusal):
        topic = decline_topic or "soft_decline_income"
        return SemanticResult(
            message_type="objection",
            summary=f"decline -> rebut once ({topic})",
            has_unresolved_interrupt=True,
            interrupt_type="objection",
            interrupt_topic=topic,
            interrupt_text=text,
            retrieval_query=text,
            retrieval_topics=[topic],
            confidence=0.85,
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
    if is_social_only(normalized):
        return SemanticResult(
            message_type="unclear",
            summary="candidate sent only a social acknowledgement",
            current_goal_satisfied=False,
            has_unresolved_interrupt=False,
            interrupt_type="none",
            interrupt_topic=None,
            interrupt_text=None,
            retrieval_query=text,
            retrieval_topics=[],
            confidence=0.78,
        )
    if is_generic_short_question(normalized):
        return SemanticResult(
            message_type="unclear",
            summary="candidate sent an unclear short question",
            current_goal_satisfied=False,
            has_unresolved_interrupt=True,
            interrupt_type="unclear",
            interrupt_topic="unclear",
            interrupt_text=text,
            retrieval_topics=[],
            evidence="short unclear question",
            confidence=0.7,
        )

    if stage == "interest_check":
        if (agreement or booking_intent) and not has_question and not has_objection:
            facts["interest_confirmed"] = True
            facts["interest_status"] = "interested"
            if booking_intent:
                facts["interview_interest"] = True
            return stage_answer(text, facts, "interest confirmed")
    elif stage == "age_check":
        # Guard: "14 про макс" / "11 айфон" are phone models, not the candidate's age.
        age = None if looks_like_phone_context(normalized) else extract_age(normalized)
        if age is not None and age >= 18:
            facts["age"] = age
            facts["age_confirmed"] = True
            facts["qualification_status"] = "age_ok"
            return stage_answer(text, facts, "age provided")
        # «Скоро 18»: 17 лет ИЛИ явное «через N дней / скоро / будет 18» — не теряем
        # кандидатку, а уходим в age_pending_18 спросить дату рождения.
        if (age is not None and age == 17) or (age is None and is_turning_18_soon(normalized)):
            if age is not None:
                facts["age"] = age
            facts["age_confirmed"] = False
            facts["qualification_status"] = "pending_18"
            return SemanticResult(
                message_type="stage_answer",
                summary="candidate turns 18 soon -> ask birthday",
                current_goal_satisfied=False,
                has_unresolved_interrupt=False,
                facts=SemanticFacts.model_validate(facts),
                retrieval_query=text,
                evidence="turning 18 soon",
                confidence=0.85,
            )
        if age is not None:  # younger than 17 -> genuinely underage
            facts["age"] = age
            facts["age_confirmed"] = False
            facts["qualification_status"] = "underage"
            return hard_refusal_result(text, "underage")
        # «Да / конечно / есть» в ответ на наш вопрос, упоминавший «18» («тебе уже
        # есть 18?») — засчитываем 18+ без числа. Реальные диалоги застревали тут:
        # модель спрашивала про 18, девочка отвечала «Да», а воронка тупо
        # переспрашивала канонный «сколько тебе лет?». Документы проверит собес.
        if (
            agreement
            and not has_question
            and not has_objection
            and "18" in last_outbound_bot_text(state)
        ):
            facts["age"] = 18
            facts["age_confirmed"] = True
            facts["qualification_status"] = "age_ok"
            return stage_answer(text, facts, "18+ confirmed affirmatively")
    elif stage == "age_pending_18":
        birthday_at = extract_birthday_18_at(normalized)
        if birthday_at is not None:
            facts["birthday"] = text.strip()[:80]
            facts["birthday_18_at"] = birthday_at.isoformat()
            facts["qualification_status"] = "pending_18"
            return stage_answer(text, facts, "birthday captured for 18th-birthday follow-up")
    elif stage == "salary_schedule_offer":
        if (agreement or booking_intent) and not has_question and not has_objection:
            facts["salary_schedule_interest"] = True
            if booking_intent:
                facts["interview_interest"] = True
            return stage_answer(text, facts, "salary/schedule interest confirmed")
        if agreement and (has_question or has_objection):
            facts["salary_schedule_interest"] = True
    elif stage == "post_equipment_questions_check":
        if questions_are_resolved(normalized) or booking_intent:
            facts["questions_resolved"] = True
            if booking_intent:
                facts["interview_interest"] = True
            return stage_answer(text, facts, "questions resolved")
        if topics:
            interrupt_type = "objection" if any(topic in OBJECTION_TOPICS for topic in topics) else "question"
            return SemanticResult(
                message_type="objection" if interrupt_type == "objection" else "interrupt_question",
                summary="candidate asks a question after info materials",
                current_goal_satisfied=False,
                has_unresolved_interrupt=True,
                interrupt_type=interrupt_type,
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
        contextual_profile = contextual_profile_answer(text, normalized) if not has_question and not has_objection else None
        if contextual_profile:
            facts.update(contextual_profile)
            return stage_answer(text, facts, "profile info inferred from contextual answer")
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "room_available_check":
        room = extract_room_available(normalized)
        if room is not None:
            facts["room_available"] = room
            facts["room_note"] = text
            return stage_answer(text, facts, "room availability answered") if room else partial_or_objection(text, facts, "room_not_available")
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "equipment_phone_check":
        if mentions_equipment(normalized) and not has_question:
            facts["equipment_available"] = True
        phone_model = extract_phone_model(text)
        if phone_model:
            facts["phone_model"] = phone_model
            eligible = assess_phone_eligibility(phone_model)
            if eligible is not None:
                facts["phone_eligible"] = eligible
            return stage_answer(text, facts, "phone model provided")
        if mentions_phone_brand(normalized) and not has_question:
            return partial_answer(text, facts, ["phone_requirements"])
        # Не называет конкретную модель, но утверждает, что телефон есть/обычный
        # ("это мой телефон", "обычный", "норм", "пользуюсь им"). Не зацикливаем
        # переспрос — по решению: непонятная модель = считаем, что подходит.
        if (
            contains_any(
                normalized,
                ("мой телефон", "это мой", "обычный", "обычн", "нормальн", "норм", "пользуюсь", "современн", "новый", "свежий"),
            )
            and not has_question
            and not has_objection
        ):
            facts["phone_model"] = (text.strip()[:80] or "не уточнила")
            facts["phone_eligible"] = True
            return stage_answer(text, facts, "model unspecified, assumed fit")
        if facts.get("equipment_available"):
            return partial_answer(text, facts, ["equipment"])
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "equipment_pc_fallback_check":
        pc = extract_pc_webcam_available(text)
        if pc is not None:
            facts["pc_webcam_available"] = pc
            return stage_answer(text, facts, "pc/webcam availability answered")
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "interview_offer":
        if (agreement or booking_intent) and not has_question and not has_objection:
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
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "interview_day_check":
        day = extract_interview_day(text)
        if day and not is_negative(normalized):
            facts["interview_day_confirmed"] = False
            facts["interview_day"] = day
            return stage_answer(text, facts, "custom interview day provided")
        if is_negative(normalized):
            day = day or text
            facts["interview_day_confirmed"] = False
            facts["interview_day"] = day
            return stage_answer(text, facts, "tomorrow is not convenient")
        if agreement:
            facts["interview_day_confirmed"] = True
            facts["interview_day"] = "завтра"
            return stage_answer(text, facts, "tomorrow confirmed")
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
    elif stage == "interview_time_check":
        time = extract_interview_time(text)
        if time:
            time = normalize_msk_daytime_short_hour(normalized, time)
            facts["interview_time"] = time
            if time_in_range(time, 11, 18):
                return stage_answer(text, facts, "interview time selected")
            return partial_or_objection(text, facts, "time_out_of_range")
        if booking_intent and not has_question and not has_objection:
            facts["interview_interest"] = True
            return partial_answer(text, facts, [])
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

    if is_acknowledgement_only(normalized):
        return SemanticResult(
            message_type="unclear",
            summary="candidate only acknowledged previous answer",
            current_goal_satisfied=False,
            has_unresolved_interrupt=False,
            interrupt_type="none",
            interrupt_topic=None,
            interrupt_text=None,
            retrieval_query=text,
            retrieval_topics=[],
            evidence="neutral acknowledgement",
            confidence=0.8,
        )

    if has_question or has_objection:
        if stage == "interest_check" and agreement:
            facts["interest_confirmed"] = True
            facts["interest_status"] = "interested"
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


def last_outbound_bot_text(state: FunnelGraphState) -> str:
    """Текст нашего ПОСЛЕДНЕГО исходящего (что бот реально спросил последним).

    Сначала metadata.last_bot_message (его пишет action_executor каждый ход),
    затем — последняя outbound-запись из recent_messages / conversation_history.
    Нужен, чтобы понимать утвердительные ответы на переформулированные вопросы
    («тебе уже есть 18?» → «да»)."""
    metadata = dict(state.get("metadata") or {})
    last = str(metadata.get("last_bot_message") or "").strip()
    if last:
        return last
    for source in ("recent_messages", "conversation_history"):
        for item in reversed(list(state.get(source) or [])):
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction") or "").lower()
            sender = str(item.get("sender_type") or "").lower()
            if direction == "outbound" or sender in {"agent", "bot", "recruiter"}:
                body = str(item.get("body") or "").strip()
                if body:
                    return body
    return ""


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


def is_interview_booking_intent(text: str) -> bool:
    if not contains_any(text, ("собес", "собесед", "интервью", "созвон")):
        return False
    return bool(
        re.search(
            r"(^|[\s,!.?])(го|давай|можем|готова|готов|хочу|запиши|запишите|запишем|запишемся)([\s,!.?]|$)",
            text,
        )
        or "запис" in text
    )


def is_negative(text: str) -> bool:
    return bool(re.search(r"(^|[\s,!.?])(нет|неа|не удобно|неудобно|не могу)([\s,!.?]|$)", text))


def is_do_not_contact(text: str) -> bool:
    return contains_any(text, ("не пиши", "не пишите", "не беспокой", "удали", "отпиши", "не надо писать"))


def is_hard_refusal(text: str, stage: str) -> bool:
    if "вопросов нет" in text or "пока вопросов" in text:
        return False
    if re.search(r"(^|[\s,!.?])(не\s+интересно|неинтересно|не\s+актуально|отказываюсь|не\s+хочу|не\s+подходит)([\s,!.?]|$)", text):
        return True
    return text in {"нет", "нет спасибо", "неа"} and stage in {"interest_check", "salary_schedule_offer", "interview_offer"}


SOFT_DECLINE_MARKERS = (
    "спасибо, но нет",
    "спасибо но нет",
    "пока нет",
    "не думаю что",
    "вряд ли",
    "не моё",
    "не мое",
    "не горю желанием",
    "не готова пока",
    "наверное нет",
)


def is_soft_decline(text: str) -> bool:
    """A polite/soft 'no' that deserves ONE objection rebuttal (income angle),
    not an instant give-up like a hard refusal. Narrow on purpose so it doesn't
    swallow genuine questions that merely contain 'но'."""
    if re.search(r"(^|[\s,!.?])но\s+нет([\s,!.?]|$)", text):
        return True
    return contains_any(text, SOFT_DECLINE_MARKERS)


# «У меня уже есть работа» — отрабатывается углом совмещения/доп.дохода, а не
# просто деньгами; отдельная тема even_employed с собственным ответом.
ALREADY_EMPLOYED_MARKERS = (
    "есть работа",
    "уже работаю",
    "своя работа",
    "работа есть",
    "не ищу работу",
    "не ищу работы",
    "трудоустроена",
    "трудоустроен",
    "у меня работа",
)

# «Не интересует / не нужно / не надо» и т.п. — несогласие, которое отрабатывается
# денежным углом РОВНО раз (как soft decline), а не уводится в lost сразу.
NOT_INTERESTED_MARKERS = (
    "не интересует",
    "неинтересует",
    "не интересна",
    "не нужно",
    "не нужна работа",
    "не надо",
    "мне это не нужно",
)


def rebuttable_decline_topic(text: str) -> str | None:
    """Единая точка распознавания «несогласия, которое стоит отработать один раз».

    Возвращает топик возражения для отработки (`already_employed` /
    `soft_decline_income`) или None. Граф после отработки ставит
    `soft_decline_rebutted`; повторный отказ затем уходит в lost. См.
    [[soft-decline-objection-once]]."""
    if contains_any(text, ALREADY_EMPLOYED_MARKERS):
        return "already_employed"
    if is_soft_decline(text) or contains_any(text, NOT_INTERESTED_MARKERS):
        return "soft_decline_income"
    return None


def is_pause(text: str) -> bool:
    return contains_any(text, ("через час", "позже", "потом", "занята", "занят", "не сейчас", "попозже"))


def is_turning_18_soon(text: str) -> bool:
    """Кандидатка сообщает, что 18 ещё нет, но исполнится скоро («через несколько
    дней 18», «скоро 18», «будет 18 в июне»…). Требуем и «18», и маркер близкого
    будущего, чтобы не путать с уже-взрослыми."""
    if "18" not in text:
        return False
    return bool(
        re.search(
            r"(скоро|почти|вот[\s-]?вот|на дн|через\s+\w+|будет|исполн|стукнет|ещё нет|еще нет|пока нет|нет ещё|нет еще|пока 17|мне 17|только 17)",
            text,
        )
    )


RUS_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "марта": 3, "апрел": 4, "ма": 5, "мая": 5,
    "июн": 6, "июл": 7, "август": 8, "авгус": 8, "сентябр": 9, "октябр": 10,
    "ноябр": 11, "декабр": 12,
}

_RELATIVE_DAYS = (("послезавтра", 2), ("завтра", 1), ("сегодня", 0))


def extract_birthday_18_at(text: str, now: datetime | None = None) -> datetime | None:
    """Распарсить дату рождения и вернуть момент 18-летия (09:00 UTC того дня).

    Поддерживает: «через N дней», «через неделю/две недели», «завтра/послезавтра/
    сегодня», «DD.MM», «DD <месяц>». Для месяца/числа берётся БЛИЖАЙШЕЕ будущее
    вхождение (кандидатке скоро 18, значит её ближайший др и есть 18-летие)."""
    now = now or datetime.now(UTC)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)

    weeks = re.search(r"через\s+(\d+)\s*недел", text)
    if weeks:
        return today + timedelta(weeks=int(weeks.group(1)))
    if re.search(r"через\s+недел", text):
        return today + timedelta(weeks=1)
    days = re.search(r"через\s+(\d+)\s*(дн|дня|дней|день)", text)
    if days:
        return today + timedelta(days=int(days.group(1)))
    for marker, offset in _RELATIVE_DAYS:
        if marker in text:
            return today + timedelta(days=offset)

    day = month = None
    dmy = re.search(r"\b([0-3]?\d)[.\-/]([01]?\d)\b", text)
    if dmy:
        day, month = int(dmy.group(1)), int(dmy.group(2))
    else:
        dm = re.search(r"\b([0-3]?\d)\s+([а-я]+)", text)
        if dm:
            day = int(dm.group(1))
            stem = dm.group(2)
            for key, num in RUS_MONTHS.items():
                if stem.startswith(key):
                    month = num
                    break
    if day and month and 1 <= day <= 31 and 1 <= month <= 12:
        year = now.year
        try:
            candidate = today.replace(year=year, month=month, day=day)
        except ValueError:
            return None
        if candidate < today:
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                return None
        return candidate
    return None


def is_social_only(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    tokens = [token for token in cleaned.split() if token]
    if not tokens or len(tokens) > 3:
        return False
    social_tokens = {
        "привет",
        "приветик",
        "здравствуйте",
        "здравствуй",
        "добрый",
        "день",
        "вечер",
        "утро",
        "хай",
        "hello",
        "hi",
    }
    return all(token in social_tokens for token in tokens)


def is_acknowledgement_only(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
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


def infer_topics(text: str) -> list[str]:
    topic_markers = [
        ("contact_source", ("откуда", "контакт", "номер", "нашли", "нашла", "нашел", "нашёл", "аккаунт", "дв", "групп")),
        ("why_selected", ("почему", "заинтересовала", "выбрали", "подошла", "внешност", "опрят")),
        ("job_description", ("что за работа", "обязан", "требоваться", "требуется", "что делать", "суть", "предложение", "уделять внимание", "трансляц", "стрим")),
        ("nudity_onlyfans", ("огол", "голая", "onlyfans", "онлифанс", "вебкам", "интим", "эрот", "раздев", "ню")),
        ("income", ("доход", "зарплата", "зп", "платят", "сколько")),
        ("schedule", ("график", "смен", "когда работать", "совмещ", "3 дня", "три дня")),
        ("equipment", ("оборуд", "камера", "свет", "микрофон", "стримингов", "стриммингов", "компьютер", "ноут")),
        ("phone_requirements", ("телефон", "айфон", "iphone", "samsung", "самсунг", "модель", "критерии", "android", "андроид")),
        ("payment_process", ("оплат", "выплаты", "деньги", "карта", "банк", "банками", "рф карт", "после смен", "еженед", "каждую недель")),
        ("contract_gph", ("договор", "гпх", "официально", "документы", "договорен", "не официаль")),
        ("english_level", ("английск", "english", "инглиш", "язык", "переводчик", "зарубеж", "американ", "аудитор")),
        ("company_info", ("компания", "кто вы", "profitcast", "профит", "британ", "юрисдик", "2018", "на рынке", "зарубежная платформа", "снг")),
        ("company_channels", ("сайт", "соц", "соцсети", "тгк", "канал", "телеграм", "ссылк", "первый пост", "profitcast.online", "official_profitcast")),
        ("platform_info", ("платформа", "dacast", "restream", "nonolive", "twitch", "твич", "продюсерский аккаунт")),
        ("training_process", ("стажиров", "обучен", "сколько длится", "когда начать", "когда можно начать", "старт")),
        ("friend_streaming", ("подруг", "вдвоем", "вдвоём", "вместе", "одному", "одной", "привести")),
        ("theme_selection", ("тематика", "тема", "хобби", "увлеч", "рис", "танц", "макияж", "визаж", "тик ток", "тикток", "сериал", "готов", "рукодел")),
        ("meet_in_person", ("встрети", "встреча", "вживую", "в реальности", "увидеться", "повидаться", "оффлайн", "офлайн", "при встрече")),
        ("interview_process", ("собесед", "интервью", "созвон", "зум", "zoom", "google meet", "meet", "дискорд", "скачать", "онлайн", "сколько занимает")),
        ("room", ("комната", "место", "помешает", "одна")),
        ("timezone", ("мск", "москва", "москов", "часовой", "время разное", "время то разное", "краснояр", "utc", "моему времени")),
        ("privacy_anonymity", ("узнают", "друзья", "знакомые", "аноним", "зарубежной сфере", "снг аудитори", "иностранные девушки")),
        ("documents_privacy", ("паспорт", "личные документы", "личные данные", "вложен", "дата рождения", "серию", "номер можно скрыть")),
        ("exit_policy", ("отработ", "отказаться", "передум", "на год", "обязательств", "обязана", "невидимым текстом")),
        ("trust_concern", ("довер", "сомнев", "сомнительно", "странно", "опас", "слабо верится", "не верится", "не слышала", "правда")),
        ("suspicious_or_scam", ("скам", "мошен", "развод", "обман", "подозр")),
        ("no_experience", ("нет опыта", "не умею", "никогда", "без опыта", "стесня", "не работала", "не работал", "не работаю на", "мой уровень")),
        ("new_sphere_uncertainty", ("в новинку", "новая сфера", "ничего не понятно", "не уверена", "попробовать страшно")),
        ("no_time", ("нет времени", "занята", "позже", "не сейчас", "через час", "потом", "уснула")),
        # ВАЖНО: маркеры — подстроки; голое «бот» нельзя (содержится в «работа»).
        ("bot_suspicion", ("ты бот", "вы бот", "не бот", "бот?", "бот что ли", "ботом", "фейк", "робот", "нейросет", "автоответ", "ты вообще человек", "живой человек", "ты человек")),
        ("already_in_industry", ("уже этим занимаюсь", "тем же самым", "этим же занимаюсь", "уже занимаюсь этим", "уже стримлю", "я стример", "уже в этой сфере", "работаю в этой сфере", "уже в теме")),
        ("legal_concern", ("законно", "легально", "налог", "уголов", "запрещено", "по закону")),
        ("need_to_think", ("подумаю", "надо подумать", "нужно подумать", "посоветуюсь", "обсужу с")),
        ("wants_smalltalk_first", ("не про работу", "не о работе", "просто пообщаемся", "пообщаемся для начала", "сначала познакомимся", "давай познакомимся", "узнаем друг друга")),
    ]
    topics = [topic for topic, markers in topic_markers if contains_any(text, markers)]
    # Demote the broad job_description topic when a specific topic also matched, so
    # the most specific topic leads (it also drives interrupt_topic via
    # first_specific_topic) and the reply answers the actual question.
    return demote_broad_topics(topics)[:5]


def is_question_like(raw: str, text: str, topics: list[str]) -> bool:
    if "?" in raw:
        return True
    if any(topic in {"timezone", "platform_info", "company_channels", "friend_streaming", "training_process", "privacy_anonymity", "documents_privacy", "exit_policy", "meet_in_person"} for topic in topics):
        return True
    return bool(topics) and contains_any(
        text,
        ("что", "как", "сколько", "откуда", "почему", "зачем", "какой", "какая", "можно", "нужен", "нужно", "обязател", "где", "какую"),
    )


def is_generic_short_question(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE).strip()
    tokens = [token for token in cleaned.split() if token]
    if len(tokens) > 2:
        return False
    return cleaned in {"что", "чего", "в смысле", "не понял", "не поняла"}


def is_objection_like(text: str, topics: list[str]) -> bool:
    if any(topic in OBJECTION_TOPICS for topic in topics):
        return True
    if "english_level" in topics and contains_any(text, ("беда", "плохо", "слаб", "не знаю", "проблем")):
        return True
    if re.search(r"(^|[\s,!.?])но([\s,!.?]|$)", text):
        return True
    return contains_any(text, ("боюсь", "стесняюсь", "не уверена", "сомневаюсь", "не понятно", "непонятно", "проблем"))


PHONE_MODEL_AGE_FALSE_SUFFIXES = {
    "про", "pro", "макс", "max", "плюс", "plus", "ultra", "ультра",
    "мини", "mini", "se", "промакс", "прошка", "прошку", "айфон", "iphone",
}


def extract_age(text: str) -> int | None:
    tokens = text.split()
    for index, token in enumerate(tokens):
        cleaned = token.strip(".,!?:;()")
        if not re.fullmatch(r"1[4-9]|[2-6]\d", cleaned):
            continue
        prev_token = tokens[index - 1].strip(".,!?:;()") if index > 0 else ""
        next_token = tokens[index + 1].strip(".,!?:;()") if index + 1 < len(tokens) else ""
        # Skip numbers that are part of a phone model, e.g. "14 про макс", "айфон 13", "11 pro".
        if any(marker in prev_token for marker in PHONE_MODEL_MARKERS):
            continue
        if next_token in PHONE_MODEL_AGE_FALSE_SUFFIXES:
            continue
        return int(cleaned)
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
            "без вопросов",
            "вопросов больше нет",
            "больше вопросов нет",
            "вопросов не осталось",
            "не осталось вопросов",
            "по ходу разбер",
            "потом появ",
            "думаю появ",
            "все понятно",
            "всё понятно",
            "все ясно",
            "всё ясно",
            "мне понятно",
            "мне ясно",
            "понятно",
        ),
    )


def contextual_profile_answer(raw: str, text: str) -> dict[str, Any] | None:
    if answers_no_work_or_study_context(text):
        occupation = "сейчас ничем не занимается" if answers_no_current_activity(text) else "не учится и не работает"
        return {
            "profile_info": raw.strip(),
            "work_or_study": occupation,
        }
    if answers_current_activity_or_leisure(text):
        return {"profile_info": raw.strip()}
    return None


def answers_no_work_or_study_context(text: str) -> bool:
    explicit_negative = contains_any(
        text,
        (
            "не учусь",
            "не работаю",
            "не учится",
            "не работает",
            "ни учусь",
            "ни работаю",
            "нигде не учусь",
            "нигде не работаю",
            "без учебы",
            "без учёбы",
            "без работы",
            "просто отдыхаю",
            "сейчас отдыхаю",
            "ничего не делаю",
        ),
    )
    if explicit_negative:
        return True

    reminder_negative = contains_any(
        text,
        ("я же сказал", "я же говор", "уже сказал", "уже говор", "говорил уже", "говорила уже"),
    ) and contains_any(text, ("нет", "не", "никак", "ничего"))
    if reminder_negative:
        return True

    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE).strip()
    return cleaned in {"нет", "неа", "никак", "ничем", "ничего"}


def answers_no_current_activity(text: str) -> bool:
    return contains_any(
        text,
        (
            "ничего не делаю",
            "ничем не занимаюсь",
            "сейчас ничем",
            "в целом ничего",
            "вцелом ничего",
            "целом ничего",
        ),
    )


def answers_current_activity_or_leisure(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE).strip()
    tokens = [token for token in cleaned.split() if token]
    if len(tokens) < 2:
        return False
    activity_markers = (
        "балду гоня",
        "ничего особ",
        "дома сиж",
        "сижу дома",
        "в компе",
        "за компом",
        "компьютер",
        "игра",
        "залипа",
        "смотр",
        "листа",
        "скрол",
        "тик ток",
        "тикток",
        "tiktok",
        "ютуб",
        "youtube",
        "сериал",
        "аниме",
        "музык",
        "гуля",
        "рису",
        "чита",
        "сплю",
        "отдыха",
    )
    return contains_any(cleaned, activity_markers)


def looks_like_profile_info(text: str) -> bool:
    if len(text) < 8:
        return False
    work_or_study_markers = ("учусь", "студент", "студентка", "универ", "колледж", "школ", "институт", "работаю", "подрабатываю")
    hobby_context_markers = ("люблю", "хобби", "занимаюсь", "свободное", "увлекаюсь", "интересуюсь")
    return contains_any(text, work_or_study_markers) or contains_any(text, hobby_context_markers)


def extract_work_or_study(text: str) -> str | None:
    normalized = normalize_text(text)
    parts = []
    if contains_any(normalized, ("не учусь", "не учится", "ни учусь", "нигде не учусь")):
        parts.append("не учусь")
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


PHONE_MODEL_MARKERS = (
    "iphone",
    "айфон",
    "айфончик",
    "samsung",
    "самсунг",
    "галакси",
    "galaxy",
    "xiaomi",
    "сяоми",
    "ксиоми",
    "redmi",
    "редми",
    "poco",
    "поко",
    "honor",
    "хонор",
    "huawei",
    "хуавей",
    "хуавэй",
    "oneplus",
    "ванплюс",
    "pixel",
    "пиксель",
    "пиксел",
    "realme",
    "реалми",
    "vivo",
    "виво",
    "oppo",
    "оппо",
    "tecno",
    "техно",
    "infinix",
    "инфиникс",
    "nothing phone",
    "нубия",
    "nubia",
)


def mentions_phone_brand(text: str) -> bool:
    return contains_any(text, PHONE_MODEL_MARKERS)


def looks_like_phone_context(text: str) -> bool:
    """True when a number in the message belongs to a phone model, not the age."""
    if mentions_phone_brand(text):
        return True
    return contains_any(
        text,
        ("про макс", "промакс", "pro max", "прошк", "прошка", "ultra", "ультра", "plus", "плюс", "модель", "телефон"),
    )


def extract_phone_model(text: str) -> str | None:
    normalized = normalize_text(text)
    if not contains_any(normalized, PHONE_MODEL_MARKERS):
        return None
    cleaned = re.sub(r"^(у меня|телефон|модель)\s+", "", text.strip(), flags=re.IGNORECASE)
    cleaned_normalized = normalize_text(cleaned).strip(" .,!?:;")
    if cleaned_normalized in PHONE_MODEL_MARKERS:
        return None
    return cleaned.strip(" .,!?:;")[:80] or None


_IPHONE_NUM_RE = re.compile(r"(?:iphone|айфон\w*)\s*(\d{1,2})")


def assess_phone_eligibility(model_text: str) -> bool | None:
    """Deterministic safety net for the recording-device rule.

    Rule (for booking): iPhone 11+, Android released 2023+, flagship 2022+.
    We can only judge *numbered iPhones* with certainty here (iPhone 11+ = fit,
    iPhone X/8 and below = unfit). For Android the release year almost never
    lives in the model string and varies wildly, so we return None and let the
    LLM judge — and an undecidable model is treated as fit upstream (per spec:
    "непонятно какой телефон = считаем, что подходит", do not loop).
    """
    norm = normalize_text(model_text)
    match = _IPHONE_NUM_RE.search(norm)
    if match:
        try:
            num = int(match.group(1))
        except ValueError:
            num = None
        if num is not None and 1 <= num <= 20:
            return num >= 11
    if contains_any(norm, ("айфон икс", "айфон x", "iphone x", "айфон 10", "iphone 10")):
        return False  # iPhone X == 10, below the cutoff
    return None


def extract_pc_webcam_available(text: str) -> bool | None:
    """yes/no for the 'do you have a PC/laptop with a webcam?' fallback stage."""
    norm = normalize_text(text)
    mentions_pc = contains_any(
        norm,
        ("пк", "комп", "компьютер", "ноут", "ноутбук", "макбук", "macbook", "моноблок", "laptop"),
    )
    if contains_any(text, ("нет", "неа", "нету", "не могу", "отсутств", "к сожалению")) and not contains_any(
        text, ("да", "есть", "имеется")
    ):
        return False
    if mentions_pc and not is_negative(text):
        return True
    if contains_any(text, ("да", "есть", "имеется", "конечно", "ага")) and not is_negative(text):
        return True
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
    match = re.search(r"(?:^|\b)(?:на|к)\s+([12]?\d|3[01])(?:\b|$)", normalized)
    if match:
        return match.group(1)
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


def normalize_msk_daytime_short_hour(text: str, value: str) -> str:
    hour = int(value.split(":", 1)[0])
    if not (1 <= hour <= 6):
        return value
    if not contains_any(text, ("по москов", "по мск", "мск")):
        return value
    return f"{hour + 12:02d}:00"


def semantic_to_json(result: SemanticResult) -> str:
    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
