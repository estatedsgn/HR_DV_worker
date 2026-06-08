from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


class FunnelMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    direction: str = "inbound"
    sender_type: str | None = None
    body: str = ""
    sent_at: str | None = None


class FunnelAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal[
        "send_text", "send_voice", "handoff", "close_lost", "do_not_contact",
        "schedule_birthday_followup",
    ] = "send_text"
    idempotency_key: str | None = None
    birthday_at: str | None = None
    reply_group_id: str | None = None
    reply_group_index: int | None = None
    reply_group_size: int | None = None
    text: str | None = None
    delay_seconds: int | None = None
    media_path: str | None = None
    caption: str | None = None
    recording_delay_seconds: float | None = None
    duration_seconds: int | None = None
    typing_delay_min_seconds: float | None = None
    typing_delay_max_seconds: float | None = None
    reason: str | None = None


class RetrievedAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    card_key: str
    stage: str | None = None
    topic: str | None = None
    content: str
    score: float = 0.0
    match_reasons: list[str] = Field(default_factory=list)


class OutgoingMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "voice_pack", "template"] = "text"
    text: str | None = None
    voice_pack_id: str | None = None
    template_id: str | None = None


class FunnelGraphState(TypedDict, total=False):
    candidate_id: str
    lead_id: str
    dialog_id: str
    thread_id: str
    stage: str
    current_state: str
    previous_state: str | None
    resume_state: str | None
    stage_before: str
    state_before: dict[str, Any]
    status: str
    current_goal: str | None
    current_question: str | None
    pending_question: str | None
    pending_question_text: str | None
    last_bot_message: str | None
    last_user_message: str | None
    conversation_history: list[dict[str, Any]]
    interest_status: str | None
    qualification_status: str | None
    last_interrupt_type: str | None
    last_interrupt_topic: str | None
    waiting_since: str | None
    handoff_required: bool
    dialog_status: str | None
    next_stage_if_completed: str | None
    incoming_message: str | None
    timeout_event: str | None
    candidate_profile: dict[str, Any]
    sent_voice_packs: list[str]
    sent_templates: list[str]
    history_summary: str | None
    slots: dict[str, Any]
    message_batch: list[dict[str, Any]]
    recent_messages: list[dict[str, Any]]
    retrieved_cards: list[dict[str, Any]]
    faq_context: list[dict[str, Any]]
    objection_context: list[dict[str, Any]]
    voice_packs: dict[str, list[str]]
    response_rules: dict[str, Any]
    retrieved_knowledge: dict[str, Any]
    semantic_result: dict[str, Any]
    reply_result: dict[str, Any]
    controller_decision: dict[str, Any]
    orchestrator_result: dict[str, Any]
    agent_run: dict[str, Any]
    parse_errors: list[str]
    pending_actions: list[dict[str, Any]]
    reply_text: str | None
    send_reply: bool
    outgoing_messages: list[dict[str, Any]]
    next_action: dict[str, Any] | None
    intent: str | None
    metadata: dict[str, Any]


def normalize_graph_state(state: FunnelGraphState) -> FunnelGraphState:
    profile = dict(state.get("candidate_profile") or state.get("slots") or {})
    stage = normalize_stage_name(state.get("current_state") or state.get("stage"))
    metadata = dict(state.get("metadata") or {})
    return {
        **state,
        "candidate_id": state.get("candidate_id") or state.get("lead_id") or state.get("thread_id") or "local",
        "stage": stage,
        "current_state": stage,
        "previous_state": state.get("previous_state"),
        "resume_state": state.get("resume_state"),
        "stage_before": state.get("stage_before") or stage,
        "status": state.get("status") or "active",
        "dialog_status": state.get("dialog_status") or state.get("status") or "active",
        "candidate_profile": profile,
        "slots": profile,
        "message_batch": list(state.get("message_batch") or []),
        "timeout_event": state.get("timeout_event") or metadata.get("timeout_event"),
        "recent_messages": list(state.get("recent_messages") or []),
        "retrieved_cards": list(state.get("retrieved_cards") or []),
        "faq_context": list(state.get("faq_context") or []),
        "objection_context": list(state.get("objection_context") or []),
        "voice_packs": dict(state.get("voice_packs") or {}),
        "response_rules": dict(state.get("response_rules") or {}),
        "retrieved_knowledge": dict(state.get("retrieved_knowledge") or {}),
        "semantic_result": dict(state.get("semantic_result") or {}),
        "reply_result": dict(state.get("reply_result") or {}),
        "controller_decision": dict(state.get("controller_decision") or {}),
        "orchestrator_result": dict(state.get("orchestrator_result") or {}),
        "agent_run": dict(state.get("agent_run") or {}),
        "parse_errors": list(state.get("parse_errors") or []),
        "sent_voice_packs": list(state.get("sent_voice_packs") or []),
        "sent_templates": list(state.get("sent_templates") or []),
        "pending_actions": list(state.get("pending_actions") or []),
        "reply_text": state.get("reply_text"),
        "send_reply": bool(state.get("send_reply", True)),
        "outgoing_messages": list(state.get("outgoing_messages") or []),
        "next_action": state.get("next_action"),
        "intent": state.get("intent"),
        "last_bot_message": state.get("last_bot_message") or metadata.get("last_bot_message"),
        "last_user_message": state.get("last_user_message") or metadata.get("last_user_message"),
        "conversation_history": list(state.get("conversation_history") or metadata.get("conversation_history") or []),
        "interest_status": state.get("interest_status") or metadata.get("interest_status"),
        "qualification_status": state.get("qualification_status") or metadata.get("qualification_status"),
        "last_interrupt_type": state.get("last_interrupt_type") or metadata.get("last_interrupt_type"),
        "last_interrupt_topic": state.get("last_interrupt_topic") or metadata.get("last_interrupt_topic"),
        "waiting_since": state.get("waiting_since") or metadata.get("waiting_since"),
        "handoff_required": bool(state.get("handoff_required") or metadata.get("handoff_required") or False),
        "metadata": metadata,
    }


def inbound_message_batch(state: FunnelGraphState) -> list[dict[str, Any]]:
    if state.get("timeout_event") and not str(state.get("incoming_message") or "").strip():
        return []
    batch = [
        dict(item)
        for item in list(state.get("message_batch") or [])
        if item.get("direction") == "inbound" and str(item.get("body") or "").strip()
    ]
    if batch:
        return batch
    incoming = str(state.get("incoming_message") or "").strip()
    if incoming:
        return [{"direction": "inbound", "sender_type": "lead", "body": incoming}]
    return []


def combined_inbound_text(state: FunnelGraphState) -> str:
    if state.get("timeout_event") and not str(state.get("incoming_message") or "").strip():
        return ""
    batch = inbound_message_batch(state)
    if batch:
        return "\n".join(str(item.get("body") or "").strip() for item in batch if str(item.get("body") or "").strip())
    recent = list(state.get("recent_messages") or [])
    for item in reversed(recent):
        if item.get("direction") == "inbound" and str(item.get("body") or "").strip():
            return str(item["body"]).strip()
    return ""


def latest_inbound_text(state: FunnelGraphState) -> str:
    return combined_inbound_text(state)


def normalize_stage_name(stage: object) -> str:
    value = str(stage or "interest_check").strip()
    compatibility = {
        "new": "interest_check",
        "NEW_LEAD": "interest_check",
        "SEND_FIRST_MESSAGE": "interest_check",
        "WAIT_FIRST_REPLY": "interest_check",
        "ASK_AGE": "age_check",
        "SEND_WORK_VOICES": "work_intro_delivery",
        "OFFER_SALARY_INFO": "salary_schedule_offer",
        "SEND_SALARY_AND_SCHEDULE_INFO": "salary_schedule_delivery",
        "ASK_ANY_QUESTIONS": "post_equipment_questions_check",
        "ASK_PERSONAL_CONTEXT": "profile_theme_check",
        "SUPPORT_SMALLTALK": "support_smalltalk",
        "ASK_ROOM_AVAILABLE": "room_available_check",
        "ASK_PHONE_MODEL": "equipment_phone_check",
        "OFFER_INTERVIEW": "interview_offer",
        "COLLECT_NAME_AND_PHONE": "contact_collection",
        "ASK_INTERVIEW_DAY": "interview_day_check",
        "ASK_TIME_11_18": "interview_time_check",
        "ASK_CUSTOM_TIME": "interview_custom_time",
        "HANDOFF_TO_HUMAN": "human_handoff",
        "bootstrap": "interest_check",
        "waiting_intro_reply": "interest_check",
        "waiting_name": "interest_check",
        "handoff": "human_handoff",
        "closed": "lost",
        "room_check": "room_available_check",
        "room_available": "room_available_check",
    }
    return compatibility.get(value, value)
