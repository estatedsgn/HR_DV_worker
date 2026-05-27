from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.services.funnel_graph.funnel_policy import (
    ACTION_STAGE_TO_WAITING_STAGE,
    CANDIDATE_PROFILE_FIELDS,
    TERMINAL_STAGES,
    can_transition,
    get_stage_policy,
    normalize_candidate_profile,
    next_stage_if_requirement_met,
    policy_state_patch,
    stage_requirement_met,
)
from app.services.funnel_graph.knowledge import StaticFunnelKnowledgeBase
from app.services.funnel_graph.reply import ReplyOrchestrator, ReplyResult, natural_timeout_followup
from app.services.funnel_graph.semantic import SemanticAnalyzer, SemanticResult
from app.services.funnel_graph.state import FunnelGraphState, latest_inbound_text, normalize_graph_state


def build_funnel_graph(
    *,
    knowledge: Any | None = None,
    static_knowledge: StaticFunnelKnowledgeBase | None = None,
    semantic_analyzer: SemanticAnalyzer | None = None,
    reply_orchestrator: ReplyOrchestrator | None = None,
    orchestrator: Any | None = None,
    llm_intro_classifier: Any | None = None,
    llm_faq_answerer: Any | None = None,
) -> StateGraph:
    # Compatibility args are accepted so existing gateway/tests keep constructing the graph.
    del llm_intro_classifier, llm_faq_answerer
    static_store = static_knowledge or StaticFunnelKnowledgeBase()
    use_llm = bool(getattr(orchestrator, "use_llm", True)) if orchestrator is not None else True
    fallback = bool(getattr(orchestrator, "fallback_on_llm_error", True)) if orchestrator is not None else True
    analyzer = semantic_analyzer or SemanticAnalyzer(use_llm=use_llm, fallback_on_llm_error=fallback)
    replier = reply_orchestrator or ReplyOrchestrator(use_llm=use_llm, fallback_on_llm_error=fallback)
    knowledge_source = knowledge or static_store

    graph = StateGraph(FunnelGraphState)
    graph.add_node("load_state", load_state)
    graph.add_node("semantic_analyzer", _semantic_analyzer_node(analyzer))
    graph.add_node("retrieve_knowledge", _retrieve_knowledge_node(knowledge_source, static_store))
    graph.add_node("reply_orchestrator", _reply_orchestrator_node(replier))
    graph.add_node("state_controller", state_controller)
    graph.add_node("action_executor", _action_executor_node(static_store))
    graph.add_node("save_state", save_state)
    graph.add_edge(START, "load_state")
    graph.add_edge("load_state", "semantic_analyzer")
    graph.add_edge("semantic_analyzer", "retrieve_knowledge")
    graph.add_edge("retrieve_knowledge", "reply_orchestrator")
    graph.add_edge("reply_orchestrator", "state_controller")
    graph.add_edge("state_controller", "action_executor")
    graph.add_edge("action_executor", "save_state")
    graph.add_edge("save_state", END)
    return graph


async def load_state(state: FunnelGraphState) -> FunnelGraphState:
    normalized = normalize_graph_state(state)
    profile = normalize_candidate_profile(normalized.get("candidate_profile"))
    stage = str(normalized.get("stage") or "interest_check")
    incoming = latest_inbound_text(normalized)
    metadata = dict(normalized.get("metadata") or {})
    timeout_event = normalized.get("timeout_event") or metadata.get("timeout_event")
    metadata.pop("timeout_event", None)
    metadata["graph_foundation_version"] = "semantic_funnel_v1"
    previous_state = normalized.get("previous_state") or metadata.get("previous_state")
    conversation_history = list(normalized.get("conversation_history") or metadata.get("conversation_history") or [])
    if incoming:
        conversation_history.append({"direction": "inbound", "body": incoming, "at": datetime.now(UTC).isoformat()})
    state_before = {
        "stage": stage,
        "current_state": stage,
        "candidate_profile": deepcopy(profile),
        "resume_state": normalized.get("resume_state"),
        "pending_question": normalized.get("pending_question"),
        "last_interrupt_type": normalized.get("last_interrupt_type"),
        "sent_voice_packs": list(normalized.get("sent_voice_packs") or []),
        "sent_templates": list(normalized.get("sent_templates") or []),
    }
    return {
        **normalized,
        **policy_state_patch(stage),
        "stage": stage,
        "current_state": stage,
        "previous_state": previous_state,
        "stage_before": stage,
        "state_before": state_before,
        "incoming_message": incoming,
        "timeout_event": timeout_event,
        "last_user_message": incoming or normalized.get("last_user_message"),
        "candidate_profile": profile,
        "slots": profile,
        "conversation_history": conversation_history[-80:],
        "outgoing_messages": [],
        "pending_actions": [],
        "reply_text": None,
        "send_reply": bool(normalized.get("send_reply", True)),
        "semantic_result": {},
        "reply_result": {},
        "controller_decision": {},
        "parse_errors": [],
        "metadata": metadata,
    }


def _semantic_analyzer_node(analyzer: SemanticAnalyzer):
    async def semantic_analyzer(state: FunnelGraphState) -> FunnelGraphState:
        try:
            result = await analyzer.run(state)
            parse_errors = list(state.get("parse_errors") or [])
        except Exception as exc:
            parse_errors = [*list(state.get("parse_errors") or []), f"semantic_analyzer:{exc}"]
            result = SemanticResult(
                message_type="unclear",
                summary="semantic analyzer failed",
                has_unresolved_interrupt=True,
                interrupt_type="unclear",
                interrupt_topic="parse_error",
                interrupt_text=str(state.get("incoming_message") or ""),
                retrieval_query=str(state.get("incoming_message") or ""),
                confidence=0.0,
            )
        return {"semantic_result": result.model_dump(), "parse_errors": parse_errors}

    return semantic_analyzer


def _retrieve_knowledge_node(knowledge_source: Any, static_store: StaticFunnelKnowledgeBase):
    async def retrieve_knowledge(state: FunnelGraphState) -> FunnelGraphState:
        semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
        query = semantic.retrieval_query or str(state.get("incoming_message") or "")
        topics = list(semantic.retrieval_topics or [])
        static_payload = static_store.retrieve(
            incoming_message=str(state.get("incoming_message") or ""),
            current_stage=str(state.get("stage") or "interest_check"),
            recent_messages=list(state.get("recent_messages") or []),
            retrieval_query=query,
            retrieval_topics=topics,
        )
        faq_context = list(static_payload.get("faq_context") or [])
        objection_context = list(static_payload.get("objection_context") or [])
        retrieved = dict(static_payload.get("retrieved_knowledge") or {})

        if hasattr(knowledge_source, "retrieve_answers"):
            cards = await knowledge_source.retrieve_answers(
                query=query,
                stage=str(state.get("stage") or "interest_check"),
                topics=topics,
                top_k=5,
            )
            card_context = [card.model_dump() if hasattr(card, "model_dump") else dict(card) for card in cards]
            for card in card_context:
                item = {
                    "topic": card.get("topic") or card.get("card_key"),
                    "answer": card.get("content"),
                    "content": card.get("content"),
                    "score": card.get("score"),
                    "source": "knowledge_cards",
                }
                faq_context.append(item)
            retrieved["cards"] = card_context

        retrieved["knowledge_found"] = bool(faq_context or objection_context)
        retrieved["first_touch_message"] = static_store.template("first_touch_message")
        return {
            "faq_context": faq_context[:6],
            "objection_context": objection_context[:6],
            "voice_packs": dict(static_payload.get("voice_packs") or {}),
            "response_rules": dict(static_payload.get("response_rules") or {}),
            "retrieved_knowledge": retrieved,
            "metadata": {**dict(state.get("metadata") or {}), "templates": dict(static_payload.get("templates") or {})},
        }

    return retrieve_knowledge


def _reply_orchestrator_node(replier: ReplyOrchestrator):
    async def reply_orchestrator(state: FunnelGraphState) -> FunnelGraphState:
        try:
            result = await replier.run(state)
            parse_errors = list(state.get("parse_errors") or [])
        except Exception as exc:
            parse_errors = [*list(state.get("parse_errors") or []), f"reply_orchestrator:{exc}"]
            question = str(state.get("pending_question_text") or state.get("current_question") or "Уточни, пожалуйста.")
            result = ReplyResult(
                send_reply=True,
                outgoing_messages=[{"type": "text", "text": question}],  # type: ignore[list-item]
                reply_text=question,
                summary="reply orchestrator failed",
                confidence=0.0,
            )
        return {
            "reply_result": result.model_dump(),
            # Compatibility with older reports.
            "orchestrator_result": {"understanding": state.get("semantic_result") or {}, "reply": result.model_dump()},
            "parse_errors": parse_errors,
        }

    return reply_orchestrator


async def state_controller(state: FunnelGraphState) -> FunnelGraphState:
    current_stage = str(state.get("stage") or "interest_check")
    policy = get_stage_policy(current_stage)
    semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
    reply = ReplyResult.model_validate(state.get("reply_result") or {})
    profile = normalize_candidate_profile(state.get("candidate_profile"))
    patch_values = non_null_facts(semantic)
    patch_values = {field: value for field, value in patch_values.items() if field in CANDIDATE_PROFILE_FIELDS}
    profile.update(patch_values)

    target_stage = current_stage
    invalid_reason: str | None = None
    handoff_required = bool(reply.handoff_required or state.get("handoff_required"))
    send_reply = bool(reply.send_reply)
    outgoing = [normalize_outgoing_message(message) for message in reply.outgoing_messages]
    completed_by_fields = (
        semantic.message_type != "empty"
        and not semantic.has_unresolved_interrupt
        and stage_requirement_met(current_stage, profile)
    )

    if semantic.message_type == "do_not_contact":
        target_stage = "do_not_contact"
        outgoing = []
        send_reply = False
    elif semantic.message_type == "hard_refusal":
        target_stage = "lost"
    elif handoff_required:
        target_stage = "human_handoff"
    elif semantic.message_type != "empty" and policy.stage_type == "waiting":
        if semantic.current_goal_satisfied and not semantic.has_unresolved_interrupt:
            target_stage = next_stage_if_requirement_met(current_stage, profile)
        elif completed_by_fields:
            target_stage = next_stage_if_requirement_met(current_stage, profile)
        elif semantic.message_type == "partial_answer":
            target_stage = next_stage_if_requirement_met(current_stage, profile)
        else:
            target_stage = current_stage

    if not can_transition(current_stage, target_stage):
        invalid_reason = f"transition_not_allowed:{current_stage}->{target_stage}"
        target_stage = current_stage
    elif target_stage != current_stage and target_stage not in TERMINAL_STAGES and not stage_requirement_met(current_stage, profile):
        invalid_reason = f"stage_requirement_not_met:{current_stage}"
        target_stage = current_stage

    if invalid_reason:
        outgoing = [{"type": "text", "text": policy.current_question, "voice_pack_id": None}] if policy.current_question else []
        send_reply = True
    elif target_stage == "lost" and not outgoing:
        outgoing = [{"type": "text", "text": template_from_state(state, "lost_message") or "Поняла, не буду отвлекать.", "voice_pack_id": None}]
    elif target_stage == "human_handoff":
        if not outgoing:
            outgoing = [{"type": "text", "text": handoff_text(profile), "voice_pack_id": None}]
    elif get_stage_policy(target_stage).stage_type == "action":
        # Action executor owns voice/template/smalltalk sends.
        outgoing = []
        send_reply = True
    elif target_stage != current_stage:
        if completed_by_fields:
            outgoing = []
        next_question = get_stage_policy(target_stage).current_question
        if next_question and not outgoing_contains(outgoing, next_question):
            outgoing.append({"type": "text", "text": next_question, "voice_pack_id": None})
    elif not outgoing and policy.current_question and semantic.message_type != "empty":
        outgoing.append({"type": "text", "text": policy.current_question, "voice_pack_id": None})

    metadata = dict(state.get("metadata") or {})
    if invalid_reason:
        metadata["controller_invalid_transition"] = invalid_reason
    metadata["previous_state"] = current_stage if target_stage != current_stage else state.get("previous_state")
    metadata["last_user_message"] = state.get("last_user_message")
    if semantic.has_unresolved_interrupt:
        metadata["last_interrupt_type"] = semantic.interrupt_type
        metadata["last_interrupt_topic"] = semantic.interrupt_topic
        metadata["resume_state"] = current_stage
        if semantic.interrupt_type in {"question", "objection"} and policy.current_question:
            metadata["awaiting_interrupt_followup"] = True
            metadata["interrupt_followup_stage"] = current_stage
            metadata["interrupt_followup_question"] = policy.current_question
            metadata["interrupt_followup_started_at"] = datetime.now(UTC).isoformat()
            metadata["interrupt_followup_timeout_seconds"] = 60
    elif target_stage != current_stage:
        metadata["last_interrupt_type"] = None
        metadata["last_interrupt_topic"] = None
        metadata["resume_state"] = None
        clear_interrupt_followup(metadata)
    elif state.get("timeout_event") == "interrupt_followup":
        if metadata.get("awaiting_interrupt_followup"):
            metadata["interrupt_followup_count"] = int(metadata.get("interrupt_followup_count") or 0) + 1
        clear_interrupt_followup(metadata, keep_count=True)
    elif not semantic.has_unresolved_interrupt and semantic.message_type != "empty":
        clear_interrupt_followup(metadata)
    metadata["handoff_required"] = handoff_required or target_stage == "human_handoff"

    if profile.get("interest_status"):
        metadata["interest_status"] = profile.get("interest_status")
    if profile.get("qualification_status"):
        metadata["qualification_status"] = profile.get("qualification_status")

    controller_decision = {
        "stage_before": current_stage,
        "target_stage": target_stage,
        "transition_allowed": invalid_reason is None,
        "invalid_reason": invalid_reason,
        "applied_patch": patch_values,
        "blocked_by_interrupt": bool(semantic.has_unresolved_interrupt),
    }

    return {
        "stage": target_stage,
        "current_state": target_stage,
        "previous_state": current_stage if target_stage != current_stage else state.get("previous_state"),
        "resume_state": metadata.get("resume_state"),
        "status": status_for_stage(target_stage),
        "dialog_status": status_for_stage(target_stage),
        "candidate_profile": profile,
        "slots": profile,
        "send_reply": send_reply,
        "outgoing_messages": [normalize_outgoing_message(message) for message in outgoing],
        "reply_text": "\n\n".join(str(message.get("text")) for message in outgoing if message.get("type") == "text" and message.get("text")) or None,
        "last_interrupt_type": metadata.get("last_interrupt_type"),
        "last_interrupt_topic": metadata.get("last_interrupt_topic"),
        "handoff_required": bool(metadata.get("handoff_required")),
        "controller_decision": controller_decision,
        **policy_state_patch(target_stage),
        "metadata": metadata,
    }


def clear_interrupt_followup(metadata: dict[str, Any], *, keep_count: bool = False) -> None:
    count = metadata.get("interrupt_followup_count") if keep_count else None
    for key in (
        "awaiting_interrupt_followup",
        "interrupt_followup_stage",
        "interrupt_followup_question",
        "interrupt_followup_started_at",
        "interrupt_followup_timeout_seconds",
        "timeout_event",
    ):
        metadata.pop(key, None)
    if keep_count and count is not None:
        metadata["interrupt_followup_count"] = count
    elif not keep_count:
        metadata.pop("interrupt_followup_count", None)


def _action_executor_node(knowledge: StaticFunnelKnowledgeBase):
    async def action_executor(state: FunnelGraphState) -> FunnelGraphState:
        stage = str(state.get("stage") or "interest_check")
        policy = get_stage_policy(stage)
        outgoing = [normalize_outgoing_message(message) for message in list(state.get("outgoing_messages") or [])]
        sent_voice_packs = list(state.get("sent_voice_packs") or [])
        sent_templates = list(state.get("sent_templates") or [])
        profile = normalize_candidate_profile(state.get("candidate_profile"))

        if policy.stage_type == "action":
            action_stage = stage
            if stage == "support_smalltalk":
                outgoing.append({"type": "text", "text": smalltalk_text(profile), "voice_pack_id": None})
                profile["smalltalk_done"] = True
            if policy.voice_pack_id and policy.voice_pack_id not in sent_voice_packs:
                outgoing.append({"type": "voice_pack", "text": None, "voice_pack_id": policy.voice_pack_id})
                sent_voice_packs.append(policy.voice_pack_id)
            if policy.template_id and policy.template_id not in sent_templates:
                outgoing.append(
                    {
                        "type": "text",
                        "text": knowledge.template(policy.template_id),
                        "voice_pack_id": None,
                        "template_id": policy.template_id,
                    }
                )
                sent_templates.append(policy.template_id)
            next_stage = ACTION_STAGE_TO_WAITING_STAGE[stage]
            next_question = get_stage_policy(next_stage).current_question
            if next_question:
                if action_stage == "support_smalltalk" and outgoing and outgoing[-1].get("type") == "text":
                    outgoing[-1]["text"] = combine_text_and_question(str(outgoing[-1].get("text") or ""), next_question)
                else:
                    outgoing.append({"type": "text", "text": next_question, "voice_pack_id": None})
            stage = next_stage

        pending_actions = pending_actions_from_outgoing(state, outgoing)
        metadata = dict(state.get("metadata") or {})
        last_bot_message = "\n\n".join(str(message.get("text")) for message in outgoing if message.get("type") == "text" and message.get("text")) or state.get("last_bot_message")
        metadata["last_bot_message"] = last_bot_message
        return {
            "stage": stage,
            "current_state": stage,
            "status": status_for_stage(stage),
            "dialog_status": status_for_stage(stage),
            **policy_state_patch(stage),
            "candidate_profile": profile,
            "slots": profile,
            "sent_voice_packs": sent_voice_packs,
            "sent_templates": sent_templates,
            "outgoing_messages": outgoing if state.get("send_reply", True) else [],
            "pending_actions": pending_actions,
            "reply_text": last_bot_message,
            "last_bot_message": last_bot_message,
            "metadata": metadata,
        }

    return action_executor


async def save_state(state: FunnelGraphState) -> FunnelGraphState:
    metadata: dict[str, Any] = dict(state.get("metadata") or {})
    metadata["last_graph_node"] = "save_state"
    history = list(state.get("conversation_history") or [])
    for message in state.get("outgoing_messages") or []:
        if message.get("type") == "text" and message.get("text"):
            history.append({"direction": "outbound", "body": message["text"], "at": datetime.now(UTC).isoformat()})
        elif message.get("type") == "voice_pack" and message.get("voice_pack_id"):
            history.append({"direction": "outbound", "body": f"[voice_pack: {message['voice_pack_id']}]", "at": datetime.now(UTC).isoformat()})
    metadata["conversation_history"] = history[-80:]
    agent_run = {
        "candidate_id": state.get("candidate_id"),
        "incoming_message": state.get("incoming_message"),
        "stage_before": state.get("stage_before"),
        "state_before": state.get("state_before"),
        "semantic_result": state.get("semantic_result") or {},
        "retrieved_knowledge": state.get("retrieved_knowledge") or {},
        "reply_result": state.get("reply_result") or {},
        "controller_decision": state.get("controller_decision") or {},
        "orchestrator_result": state.get("orchestrator_result") or {},
        "outgoing_messages": state.get("outgoing_messages") or [],
        "pending_actions": state.get("pending_actions") or [],
        "stage_after": state.get("stage"),
        "state_after": {
            "stage": state.get("stage"),
            "current_state": state.get("current_state"),
            "resume_state": state.get("resume_state"),
            "candidate_profile": state.get("candidate_profile") or {},
            "sent_voice_packs": state.get("sent_voice_packs") or [],
            "sent_templates": state.get("sent_templates") or [],
        },
        "parse_errors": state.get("parse_errors") or [],
    }
    return {"metadata": metadata, "conversation_history": metadata["conversation_history"], "agent_run": agent_run}


def status_for_stage(stage: str) -> str:
    if stage == "human_handoff":
        return "handoff"
    if stage in {"ready_for_interview", "lost", "do_not_contact"}:
        return "closed"
    return "active"


def non_null_facts(semantic: SemanticResult) -> dict[str, Any]:
    return {key: value for key, value in semantic.facts.model_dump().items() if value is not None}


def normalize_outgoing_message(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        message = message.model_dump()
    raw = dict(message or {})
    return {
        "type": raw.get("type") or "text",
        "text": raw.get("text"),
        "voice_pack_id": raw.get("voice_pack_id"),
        "template_id": raw.get("template_id"),
    }


def outgoing_contains(outgoing: list[Any], text: str) -> bool:
    return any(text in str(normalize_outgoing_message(message).get("text") or "") for message in outgoing)


def template_from_state(state: FunnelGraphState, template_id: str) -> str:
    templates = dict((state.get("metadata") or {}).get("templates") or {})
    return str(templates.get(template_id) or "")


def smalltalk_text(profile: dict[str, Any]) -> str:
    hobbies = str(profile.get("hobbies") or profile.get("profile_info") or "").lower()
    if any(marker in hobbies for marker in ("рис", "карти", "макияж", "крас")):
        return "Классно, под такие увлечения обычно легко подобрать тему для эфиров."
    if any(marker in hobbies for marker in ("учусь", "работ", "практик")):
        return "Поняла, у нас как раз гибкий формат, его можно совмещать с учёбой или работой."
    return "Поняла, спасибо, это поможет подобрать подходящую тематику."


def combine_text_and_question(text: str, question: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return question
    separator = " " if cleaned.endswith((".", "!", "?")) else ". "
    return f"{cleaned}{separator}{question}"


def handoff_text(profile: dict[str, Any]) -> str:
    day = normalize_interview_day(profile.get("interview_day") or ("завтра" if profile.get("interview_day_confirmed") else None))
    time = profile.get("interview_time") or profile.get("custom_interview_datetime") or "удобное время"
    return f"Записала, передам данные менеджеру. Собеседование: {day}, {time}."


def normalize_interview_day(day: Any) -> str:
    text = str(day or "").strip()
    normalized = text.lower().replace(".", "")
    if normalized in {"tomorrow", "tmr", "завтра"}:
        return "завтра"
    return text or "выбранный день"


def pending_actions_from_outgoing(state: FunnelGraphState, outgoing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not state.get("send_reply", True):
        if state.get("stage") == "lost":
            return [{"type": "close_lost", "reason": "candidate_refused"}]
        if state.get("stage") == "do_not_contact":
            return [{"type": "do_not_contact", "reason": "candidate_asked_not_to_contact"}]
        return []
    actions = []
    thread_id = state.get("thread_id") or state.get("candidate_id") or "local"
    for index, message in enumerate(outgoing):
        if message.get("type") == "voice_pack":
            actions.append(
                {
                    "type": "send_voice",
                    "caption": f"[voice_pack: {message.get('voice_pack_id')}]",
                    "idempotency_key": f"{thread_id}:{state.get('stage')}:{index}:{message.get('voice_pack_id')}",
                }
            )
            continue
        if message.get("type") != "text" or not message.get("text"):
            continue
        actions.append(
            {
                "type": "send_text",
                "text": message["text"],
                "idempotency_key": f"{thread_id}:{state.get('stage')}:{index}:{hash(str(message.get('text')))}",
            }
        )
    if state.get("stage") == "lost":
        actions.append({"type": "close_lost", "reason": "candidate_refused"})
    elif state.get("stage") == "human_handoff":
        actions.append({"type": "handoff", "reason": "funnel_requested_handoff"})
    followup_action = interrupt_followup_action(state)
    if followup_action:
        actions.append(followup_action)
    return actions


def interrupt_followup_action(state: FunnelGraphState) -> dict[str, Any] | None:
    if not state.get("send_reply", True):
        return None
    if state.get("stage") in TERMINAL_STAGES:
        return None
    if state.get("timeout_event"):
        return None
    metadata = dict(state.get("metadata") or {})
    if not metadata.get("awaiting_interrupt_followup"):
        return None
    question = str(
        metadata.get("interrupt_followup_question")
        or state.get("pending_question_text")
        or state.get("current_question")
        or ""
    ).strip()
    if not question:
        return None
    delay_seconds = int(metadata.get("interrupt_followup_timeout_seconds") or 60)
    started_at = str(metadata.get("interrupt_followup_started_at") or "unknown").replace(":", "-")
    thread_id = state.get("thread_id") or state.get("candidate_id") or "local"
    return {
        "type": "send_text",
        "text": natural_timeout_followup(question, metadata),
        "delay_seconds": max(1, delay_seconds),
        "idempotency_key": f"{thread_id}:{state.get('stage')}:interrupt_followup:{started_at}",
    }
