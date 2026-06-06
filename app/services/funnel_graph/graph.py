from __future__ import annotations

import hashlib
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
from app.services.funnel_graph.state import (
    FunnelGraphState,
    combined_inbound_text,
    inbound_message_batch,
    normalize_graph_state,
)


# Short acknowledgement/bridge lines sent as their own message right before the
# next stage question, so a stage transition feels human instead of abrupt.
TRANSITION_BRIDGES: dict[tuple[str, str], str] = {
    ("post_equipment_questions_check", "profile_theme_check"): "давай я уточню у тебя несколько деталей, и далее мы с тобой запишемся на собеседование",
    ("room_available_check", "equipment_phone_check"): "супер",
    ("equipment_phone_check", "interview_offer"): "нам подходит",
}


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
    graph.add_node("action_executor", _action_executor_node(static_store, replier))
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
    incoming = combined_inbound_text(normalized)
    inbound_batch = inbound_message_batch(normalized)
    metadata = dict(normalized.get("metadata") or {})
    timeout_event = normalized.get("timeout_event") or metadata.get("timeout_event")
    metadata.pop("timeout_event", None)
    metadata["graph_foundation_version"] = "semantic_funnel_v1"
    metadata["graph_started_at"] = datetime.now(UTC).isoformat()
    previous_state = normalized.get("previous_state") or metadata.get("previous_state")
    conversation_history = list(normalized.get("conversation_history") or metadata.get("conversation_history") or [])
    if inbound_batch:
        for item in inbound_batch:
            conversation_history.append(
                {
                    "direction": "inbound",
                    "body": str(item.get("body") or "").strip(),
                    "at": item.get("sent_at") or datetime.now(UTC).isoformat(),
                }
            )
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
        "message_batch": inbound_batch,
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
        metadata = dict(state.get("metadata") or {})
        run_metadata = dict(getattr(analyzer, "last_run_metadata", {}) or {})
        if run_metadata:
            metadata["semantic_metrics"] = run_metadata
            metadata["model_test_profile"] = run_metadata.get("profile")
        return {"semantic_result": result.model_dump(), "parse_errors": parse_errors, "metadata": metadata}

    return semantic_analyzer


def _retrieve_knowledge_node(knowledge_source: Any, static_store: StaticFunnelKnowledgeBase):
    async def retrieve_knowledge(state: FunnelGraphState) -> FunnelGraphState:
        semantic = SemanticResult.model_validate(state.get("semantic_result") or {})
        query = semantic.retrieval_query if semantic.retrieval_topics else ""
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
        retrieved["first_touch_message"] = static_store.first_touch(state.get("candidate_id"))
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
        metadata = dict(state.get("metadata") or {})
        run_metadata = dict(getattr(replier, "last_run_metadata", {}) or {})
        if run_metadata:
            metadata["reply_metrics"] = run_metadata
            metadata["model_test_profile"] = run_metadata.get("profile") or metadata.get("model_test_profile")
        return {
            "reply_result": result.model_dump(),
            # Compatibility with older reports.
            "orchestrator_result": {"understanding": state.get("semantic_result") or {}, "reply": result.model_dump()},
            "parse_errors": parse_errors,
            "metadata": metadata,
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
    # A plain question (not an objection/refusal) at the salary/schedule offer means
    # she is engaged: answer it and deliver the materials in the same turn instead of
    # stalling on an explicit "yes". The voice pack's recording simulation gives the
    # natural pause before the audio lands.
    offer_question_voice_advance = (
        current_stage == "salary_schedule_offer"
        and semantic.has_unresolved_interrupt
        and semantic.interrupt_type == "question"
        and semantic.message_type not in {"do_not_contact", "hard_refusal", "objection"}
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

    if offer_question_voice_advance and not handoff_required:
        profile["salary_schedule_interest"] = True
        target_stage = next_stage_if_requirement_met(current_stage, profile)

    if target_stage != current_stage and not semantic.has_unresolved_interrupt:
        target_stage = advance_through_completed_waiting_stages(target_stage, profile)

    if not can_transition_via_completed_stages(current_stage, target_stage, profile):
        invalid_reason = f"transition_not_allowed:{current_stage}->{target_stage}"
        target_stage = current_stage
    elif target_stage != current_stage and target_stage not in TERMINAL_STAGES and not stage_requirement_met(current_stage, profile):
        invalid_reason = f"stage_requirement_not_met:{current_stage}"
        target_stage = current_stage

    if invalid_reason:
        outgoing = [{"type": "text", "text": policy.current_question, "voice_pack_id": None}] if policy.current_question else []
        send_reply = True
    elif target_stage == "lost" and not outgoing:
        outgoing = [{"type": "text", "text": template_from_state(state, "lost_message") or "поняла, не буду отвлекать) хорошего дня", "voice_pack_id": None}]
    elif target_stage == "human_handoff":
        if not outgoing:
            outgoing = [{"type": "text", "text": handoff_text(profile), "voice_pack_id": None}]
    elif get_stage_policy(target_stage).stage_type == "action":
        # Action executor owns voice/template/smalltalk sends. Keep the interrupt
        # answer when we advance the offer off a question, so she gets the reply
        # first and the voices follow in the same turn.
        if not offer_question_voice_advance:
            outgoing = []
        send_reply = True
    elif target_stage != current_stage:
        if completed_by_fields:
            outgoing = []
        bridge = TRANSITION_BRIDGES.get((current_stage, target_stage))
        if bridge and not outgoing_contains(outgoing, bridge):
            outgoing.append({"type": "text", "text": bridge, "voice_pack_id": None})
        next_question = get_stage_policy(target_stage).current_question
        if next_question and not outgoing_contains(outgoing, next_question):
            outgoing.append({"type": "text", "text": next_question, "voice_pack_id": None})
    elif send_reply and not outgoing and policy.current_question and semantic.message_type != "empty":
        outgoing.append({"type": "text", "text": policy.current_question, "voice_pack_id": None})

    metadata = dict(state.get("metadata") or {})
    if invalid_reason:
        metadata["controller_invalid_transition"] = invalid_reason
    metadata["previous_state"] = current_stage if target_stage != current_stage else state.get("previous_state")
    metadata["last_user_message"] = state.get("last_user_message")
    if semantic.has_unresolved_interrupt and not offer_question_voice_advance:
        metadata["last_interrupt_type"] = semantic.interrupt_type
        metadata["last_interrupt_topic"] = semantic.interrupt_topic
        metadata["resume_state"] = current_stage
        if semantic.interrupt_type in {"question", "objection"} and policy.current_question:
            update_interrupt_streak(metadata, current_stage)
            metadata["awaiting_interrupt_followup"] = True
            metadata["interrupt_followup_stage"] = current_stage
            metadata["interrupt_followup_question"] = policy.current_question
            metadata["interrupt_followup_started_at"] = datetime.now(UTC).isoformat()
            metadata["interrupt_followup_timeout_seconds"] = 120
    elif target_stage != current_stage:
        metadata["last_interrupt_type"] = None
        metadata["last_interrupt_topic"] = None
        metadata["resume_state"] = None
        clear_interrupt_followup(metadata)
        clear_interrupt_streak(metadata)
    elif state.get("timeout_event") == "interrupt_followup":
        if metadata.get("awaiting_interrupt_followup"):
            metadata["interrupt_followup_count"] = int(metadata.get("interrupt_followup_count") or 0) + 1
        clear_interrupt_followup(metadata, keep_count=True)
        clear_interrupt_streak(metadata)
    elif not semantic.has_unresolved_interrupt and semantic.message_type != "empty":
        if send_reply or not metadata.get("awaiting_interrupt_followup"):
            clear_interrupt_followup(metadata)
            clear_interrupt_streak(metadata)
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


def advance_through_completed_waiting_stages(stage: str, profile: dict[str, Any]) -> str:
    """Skip newly-entered waiting stages whose required fields were collected in this turn."""
    seen: set[str] = set()
    while stage not in seen and stage not in TERMINAL_STAGES:
        seen.add(stage)
        policy = get_stage_policy(stage)
        if policy.stage_type != "waiting" or not stage_requirement_met(stage, profile):
            break
        next_stage = next_stage_if_requirement_met(stage, profile)
        if next_stage == stage:
            break
        stage = next_stage
    return stage


def can_transition_via_completed_stages(current_stage: str, target_stage: str, profile: dict[str, Any]) -> bool:
    if can_transition(current_stage, target_stage):
        return True
    stage = current_stage
    seen: set[str] = set()
    while stage not in seen and stage not in TERMINAL_STAGES:
        seen.add(stage)
        if not stage_requirement_met(stage, profile):
            return False
        next_stage = next_stage_if_requirement_met(stage, profile)
        if next_stage == stage or not can_transition(stage, next_stage):
            return False
        stage = next_stage
        if can_transition(stage, target_stage):
            return True
    return stage == target_stage


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


def update_interrupt_streak(metadata: dict[str, Any], stage: str) -> None:
    previous_stage = metadata.get("interrupt_streak_stage")
    previous_count = int(metadata.get("interrupt_streak_count") or 0)
    metadata["interrupt_streak_stage"] = stage
    metadata["interrupt_streak_count"] = previous_count + 1 if previous_stage == stage else 1


def clear_interrupt_streak(metadata: dict[str, Any]) -> None:
    metadata.pop("interrupt_streak_stage", None)
    metadata.pop("interrupt_streak_count", None)


def _action_executor_node(knowledge: StaticFunnelKnowledgeBase, replier: ReplyOrchestrator | None = None):
    async def action_executor(state: FunnelGraphState) -> FunnelGraphState:
        stage = str(state.get("stage") or "interest_check")
        policy = get_stage_policy(stage)
        outgoing = [normalize_outgoing_message(message) for message in list(state.get("outgoing_messages") or [])]
        sent_voice_packs = list(state.get("sent_voice_packs") or [])
        sent_templates = list(state.get("sent_templates") or [])
        profile = normalize_candidate_profile(state.get("candidate_profile"))

        if policy.stage_type == "action":
            if stage == "support_smalltalk":
                reaction = None
                if replier is not None:
                    reaction = await replier.generate_smalltalk_reaction(state)
                outgoing.append({"type": "text", "text": reaction or smalltalk_text(profile), "voice_pack_id": None})
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
                # Always keep the live reaction and the next stage question as
                # separate messages so the bot reads like a human texting.
                outgoing.append({"type": "text", "text": next_question, "voice_pack_id": None})
            stage = next_stage

        pending_actions = pending_actions_from_outgoing(state, outgoing)
        metadata = dict(state.get("metadata") or {})
        last_bot_message = "\n\n".join(str(message.get("text")) for message in outgoing if message.get("type") == "text" and message.get("text")) or state.get("last_bot_message")
        metadata["last_bot_message"] = last_bot_message
        reply_group_ids = [action.get("reply_group_id") for action in pending_actions if action.get("reply_group_id")]
        if reply_group_ids:
            metadata["last_reply_group_id"] = reply_group_ids[0]
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
    metadata["graph_total_latency_ms"] = graph_total_latency_ms(metadata.get("graph_started_at"))
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
        "reply_group_id": metadata.get("last_reply_group_id"),
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


def graph_total_latency_ms(started_at: Any) -> int | None:
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))


def normalize_outgoing_message(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        message = message.model_dump()
    raw = dict(message or {})
    return {
        "type": raw.get("type") or "text",
        "text": raw.get("text"),
        "voice_pack_id": raw.get("voice_pack_id"),
        "template_id": raw.get("template_id"),
        "delay_seconds": raw.get("delay_seconds"),
        "reply_group_id": raw.get("reply_group_id"),
        "reply_group_index": raw.get("reply_group_index"),
        "reply_group_size": raw.get("reply_group_size"),
    }


def outgoing_contains(outgoing: list[Any], text: str) -> bool:
    return any(text in str(normalize_outgoing_message(message).get("text") or "") for message in outgoing)


def template_from_state(state: FunnelGraphState, template_id: str) -> str:
    templates = dict((state.get("metadata") or {}).get("templates") or {})
    return str(templates.get(template_id) or "")


def smalltalk_text(profile: dict[str, Any]) -> str:
    hobbies = str(profile.get("hobbies") or profile.get("profile_info") or "").lower()
    if any(marker in hobbies for marker in ("рис", "карти", "макияж", "крас")):
        return "классно, под такие увлечения обычно легко подобрать тему для эфиров)"
    if any(marker in hobbies for marker in ("тикток", "tiktok", "видео", "блог", "ютуб", "youtube", "реелс", "reels")):
        return "о, это прям в тему) короткие форматы сейчас на хайпе, под такое легко подобрать стиль эфиров"
    if any(marker in hobbies for marker in ("игр", "гейм", "game", "комп")):
        return "круто, по играм как раз заходят живые эфиры с общением)"
    if any(marker in hobbies for marker in ("музык", "пою", "пение", "гитар", "танц")):
        return "вау, творческим ребятам у нас обычно особенно заходит)"
    if any(marker in hobbies for marker in ("спорт", "трен", "фитнес", "бег", "йог")):
        return "класс, энергия и движ — это прям то, что хорошо смотрится в эфирах)"
    if any(marker in hobbies for marker in ("учусь", "работ", "практик", "студент", "учеб")):
        return "поняла) у нас как раз гибкий график, отлично совмещается с учёбой или работой"
    if any(marker in hobbies for marker in ("ничего", "ничем", "не знаю", "не интерес", "скучн")):
        return "это нормально, многие так начинают) как раз вместе и подберём, что тебе зайдёт"
    variants = (
        "о, здорово, что поделилась) это поможет подобрать тему по тебе",
        "поняла тебя) подберём формат, который реально твой",
        "класс, спасибо, что рассказала) уже примерно вижу, что тебе может зайти",
    )
    key = str(profile.get("profile_info") or profile.get("hobbies") or "")
    return variants[int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % len(variants)]


def handoff_text(profile: dict[str, Any]) -> str:
    day = normalize_interview_day(profile.get("interview_day") or ("завтра" if profile.get("interview_day_confirmed") else None))
    time = profile.get("interview_time") or profile.get("custom_interview_datetime") or "удобное время"
    return f"записала тебя на {day} в {time}) передам данные менеджеру, дальше с тобой свяжутся 🥰"


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
    normalized_outgoing = [normalize_outgoing_message(message) for message in outgoing]
    reply_group_id = build_reply_group_id(state, normalized_outgoing)
    group_size = outgoing_action_count(state, normalized_outgoing)
    group_index = 0
    cumulative_delay_seconds = 0
    for index, message in enumerate(normalized_outgoing):
        if message.get("type") == "voice_pack":
            for voice_index, item in enumerate(voice_pack_action_items(state, str(message.get("voice_pack_id") or ""))):
                group_index += 1
                delay_seconds = int(item.get("delay_seconds") if item.get("delay_seconds") is not None else cumulative_delay_seconds)
                recording_delay_seconds = item.get("recording_delay_seconds")
                actions.append(
                    {
                        "type": "send_voice",
                        "media_path": item.get("media_path"),
                        "caption": item.get("caption") or f"[voice_pack: {message.get('voice_pack_id')}]",
                        "recording_delay_seconds": recording_delay_seconds,
                        "duration_seconds": item.get("duration_seconds"),
                        "delay_seconds": delay_seconds,
                        "reply_group_id": reply_group_id,
                        "reply_group_index": group_index,
                        "reply_group_size": group_size,
                        "idempotency_key": (
                            f"{reply_group_id}:{index}:{message.get('voice_pack_id')}:"
                            f"{voice_index}:{item.get('id') or stable_digest(item)}"
                        ),
                    }
                )
                cumulative_delay_seconds = max(
                    cumulative_delay_seconds,
                    delay_seconds + int(float(recording_delay_seconds or 0)),
                )
            continue
        if message.get("type") != "text" or not message.get("text"):
            continue
        group_index += 1
        delay_seconds = message.get("delay_seconds")
        if delay_seconds is None:
            delay_seconds = cumulative_delay_seconds
        typing_min, typing_max = text_typing_delay_range(state)
        actions.append(
            {
                "type": "send_text",
                "text": message["text"],
                "delay_seconds": delay_seconds,
                "typing_delay_min_seconds": typing_min,
                "typing_delay_max_seconds": typing_max,
                "reply_group_id": reply_group_id,
                "reply_group_index": group_index,
                "reply_group_size": group_size,
                "idempotency_key": f"{reply_group_id}:{index}:{stable_digest(str(message.get('text')))}",
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


def outgoing_action_count(state: FunnelGraphState, outgoing: list[dict[str, Any]]) -> int:
    total = 0
    for message in outgoing:
        if message.get("type") == "voice_pack":
            total += len(voice_pack_action_items(state, str(message.get("voice_pack_id") or "")))
        elif message.get("type") == "text" and message.get("text"):
            total += 1
    return total


def voice_pack_action_items(state: FunnelGraphState, voice_pack_id: str) -> list[dict[str, Any]]:
    raw_pack = (state.get("voice_packs") or {}).get(voice_pack_id) or []
    items: list[dict[str, Any]] = []
    if isinstance(raw_pack, list):
        for raw_item in raw_pack:
            if isinstance(raw_item, dict):
                item = dict(raw_item)
            else:
                item = {"id": str(raw_item), "caption": f"[voice] {raw_item}"}
            if item.get("media_path"):
                item.setdefault("recording_delay_seconds", 40)
            items.append(item)
    if items:
        return items
    return [{"id": voice_pack_id, "caption": f"[voice_pack: {voice_pack_id}]"}]


def text_typing_delay_range(state: FunnelGraphState) -> tuple[float, float]:
    semantic = dict(state.get("semantic_result") or {})
    if semantic.get("has_unresolved_interrupt") or semantic.get("message_type") in {"interrupt_question", "objection", "mixed"}:
        return (6.0, 10.0)
    if semantic.get("current_goal_satisfied") or semantic.get("message_type") in {"stage_answer", "partial_answer"}:
        return (2.0, 4.0)
    return (4.0, 7.0)


def build_reply_group_id(state: FunnelGraphState, outgoing: list[dict[str, Any]]) -> str:
    thread_id = state.get("thread_id") or state.get("candidate_id") or "local"
    stage = state.get("stage") or "unknown"
    payload = {
        "stage": stage,
        "incoming": state.get("incoming_message") or "",
        "message_batch": [
            {
                "id": item.get("id"),
                "body": item.get("body"),
                "sent_at": item.get("sent_at"),
            }
            for item in list(state.get("message_batch") or [])
        ],
        "outgoing": [
            {
                "type": message.get("type"),
                "text": message.get("text"),
                "voice_pack_id": message.get("voice_pack_id"),
                "template_id": message.get("template_id"),
            }
            for message in outgoing
            if message.get("type") == "voice_pack" or (message.get("type") == "text" and message.get("text"))
        ],
    }
    return f"{thread_id}:{stage}:reply:{stable_digest(payload)}"


def stable_digest(value: Any) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = repr(value)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


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
    reply_group_id = f"{thread_id}:{state.get('stage')}:interrupt_followup:{stable_digest(started_at + question)}"
    return {
        "type": "send_text",
        "text": natural_timeout_followup(question, metadata),
        "delay_seconds": max(1, delay_seconds),
        "reply_group_id": reply_group_id,
        "reply_group_index": 1,
        "reply_group_size": 1,
        "idempotency_key": f"{reply_group_id}:0",
    }
