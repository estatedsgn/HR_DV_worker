from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.inbound_event import InboundEvent
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.actions import FunnelActionExecutor
from app.services.funnel_graph.checkpoint import configured_checkpointer
from app.services.funnel_graph.graph import build_funnel_graph
from app.services.funnel_graph.intro import LLMFAQAnswerer, LLMIntroClassifier
from app.services.funnel_graph.knowledge import FunnelKnowledgeAdapter
from app.services.funnel_graph.state import FunnelGraphState, FunnelMessage
from app.services.funnel_graph.turn_buffer import message_activity_at


class LangGraphFunnelGateway:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.checkpointer = checkpointer

    async def decide_for_dialog_message(self, *, dialog_id: str, message_id: str | None) -> FunnelGraphState | None:
        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            return None
        lead = await self._get_or_create_lead(dialog)
        runtime = await self.get_or_create_runtime(lead=lead, dialog=dialog)
        # A human has taken over this dialog (handoff or explicit pause): the bot
        # must stay COMPLETELY silent — no graph run, no reply — until a human
        # explicitly resumes it (scripts/resume_funnel_dialog.py). Otherwise the
        # funnel keeps answering the lead's messages and "interjects" right in the
        # middle of the recruiter's own conversation.
        if is_bot_silenced(runtime):
            return None
        message = await self.session.get(Message, message_id) if message_id else None
        if message is None:
            message = await self._latest_inbound(dialog.id)
        if message is None or message.direction == "outbound":
            return None
        recent_messages = await self._recent_messages(dialog.id)
        message_batch = await self._inbound_batch_since_last_outbound(dialog.id) or [message]
        processed_message = latest_message_from_batch(message_batch) or message
        reply_context = await self._reply_context_map(message_batch)
        initial_state = self._initial_state(
            lead=lead,
            dialog=dialog,
            runtime=runtime,
            recent_messages=recent_messages,
            message_batch=message_batch,
            reply_context=reply_context,
        )
        async with configured_checkpointer(self.settings, self.checkpointer) as checkpointer:
            graph = build_funnel_graph(
                knowledge=FunnelKnowledgeAdapter(self.session),
                llm_intro_classifier=LLMIntroClassifier(settings=self.settings).classify,
                llm_faq_answerer=LLMFAQAnswerer(settings=self.settings).answer,
            ).compile(checkpointer=checkpointer)
            result = await graph.ainvoke(
                initial_state,
                config={"configurable": {"thread_id": runtime.thread_id}},
            )
        if await self._has_newer_inbound(dialog.id, processed_message):
            await self._mark_superseded_runtime(runtime=runtime, baseline_message=processed_message)
            return mark_superseded_result(result, baseline_message=processed_message)
        await self._persist_runtime(runtime=runtime, result=result, message=processed_message)
        await FunnelActionExecutor(self.session).execute(
            dialog=dialog,
            lead=lead,
            runtime=runtime,
            actions=actions_from_graph_result(result),
        )
        await self.session.flush()
        return result

    async def start_for_dialog(self, *, dialog_id: str) -> FunnelGraphState | None:
        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            return None
        lead = await self._get_or_create_lead(dialog)
        runtime = await self.get_or_create_runtime(lead=lead, dialog=dialog)
        initial_state = self._initial_state(
            lead=lead,
            dialog=dialog,
            runtime=runtime,
            recent_messages=[],
            message_batch=[],
        )
        async with configured_checkpointer(self.settings, self.checkpointer) as checkpointer:
            graph = build_funnel_graph(
                knowledge=FunnelKnowledgeAdapter(self.session),
                llm_intro_classifier=LLMIntroClassifier(settings=self.settings).classify,
                llm_faq_answerer=LLMFAQAnswerer(settings=self.settings).answer,
            ).compile(checkpointer=checkpointer)
            result = await graph.ainvoke(
                initial_state,
                config={"configurable": {"thread_id": runtime.thread_id}},
            )
        await self._persist_runtime(runtime=runtime, result=result, message=None)
        await FunnelActionExecutor(self.session).execute(
            dialog=dialog,
            lead=lead,
            runtime=runtime,
            actions=actions_from_graph_result(result),
        )
        await self.session.flush()
        return result

    async def restart_for_dialog(
        self,
        *,
        dialog_id: str,
        triggering_message_id: str | None = None,
        current_event_id: str | None = None,
    ) -> FunnelGraphState | None:
        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            return None
        lead = await self._get_or_create_lead(dialog)
        runtime = await self.get_or_create_runtime(lead=lead, dialog=dialog)
        now = datetime.now(UTC)
        cancelled_jobs = await self._cancel_active_outbound(
            dialog.id,
            reason="cancelled by restart command",
        )
        retired_events = await self._retire_pending_inbound_events(
            dialog.id,
            current_event_id=current_event_id,
            reason="ignored by restart command",
            now=now,
        )
        lead.qualification_status = "new"
        lead.funnel_state = "NEW_LEAD"
        lead.interest_status = None
        lead.summary = None
        lead.next_step = None
        lead.handoff_ready_at = None
        lead.lost_reason = None
        lead.do_not_contact_reason = None
        runtime.dialog_id = dialog.id
        runtime.thread_id = self._new_restart_thread_id(dialog.id)
        runtime.stage = "interest_check"
        runtime.status = "active"
        runtime.last_processed_message_id = parse_uuid(triggering_message_id)
        runtime.last_candidate_activity_at = None
        runtime.last_candidate_typing_at = None
        runtime.debounce_until = None
        runtime.current_goal = None
        runtime.metadata_json = {
            "restart_command": True,
            "restart_at": now.isoformat(),
            "telegram_username": dialog.telegram_username,
            "triggering_message_id": triggering_message_id,
            "cancelled_outbound_jobs": cancelled_jobs,
            "retired_inbound_events": retired_events,
        }
        await self.session.flush()
        return await self.start_for_dialog(dialog_id=str(dialog.id))

    async def get_or_create_runtime(self, *, lead: Lead, dialog: Dialog) -> LeadFunnelRuntime:
        result = await self.session.execute(
            select(LeadFunnelRuntime).where(LeadFunnelRuntime.lead_id == lead.id).limit(1)
        )
        runtime = result.scalar_one_or_none()
        if runtime is not None:
            return runtime
        runtime = LeadFunnelRuntime(
            lead_id=lead.id,
            dialog_id=dialog.id,
            thread_id=str(dialog.id),
            stage="interest_check",
            status="active",
            current_goal="Понять, интересно ли кандидату узнать подробности.",
            metadata_json={},
        )
        self.session.add(runtime)
        await self.session.flush()
        return runtime

    def _new_restart_thread_id(self, dialog_id: UUID) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{dialog_id}:restart-{timestamp}-{uuid4().hex[:8]}"[:120]

    async def _cancel_active_outbound(self, dialog_id: UUID, *, reason: str) -> int:
        result = await self.session.execute(
            select(OutboundJob).where(
                OutboundJob.dialog_id == dialog_id,
                OutboundJob.status.in_(["queued", "retry", "processing"]),
            )
        )
        cancelled = 0
        for job in result.scalars().all():
            job.status = "cancelled"
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.error_message = reason
            if job.message_id:
                message = await self.session.get(Message, job.message_id)
                if message is not None:
                    message.status = "cancelled"
            cancelled += 1
        await self.session.flush()
        return cancelled

    async def _retire_pending_inbound_events(
        self,
        dialog_id: UUID,
        *,
        current_event_id: str | None,
        reason: str,
        now: datetime,
    ) -> int:
        query = select(InboundEvent).where(
            InboundEvent.dialog_id == dialog_id,
            InboundEvent.status.in_(["received", "queued", "retry", "processing"]),
        )
        current_uuid = parse_uuid(current_event_id)
        if current_uuid is not None:
            query = query.where(InboundEvent.id != current_uuid)
        result = await self.session.execute(query)
        retired = 0
        for event in result.scalars().all():
            event.status = "processed"
            event.processed_at = now
            event.next_attempt_at = None
            event.lease_owner = None
            event.lease_expires_at = None
            event.error_message = reason
            retired += 1
        await self.session.flush()
        return retired

    async def _get_or_create_lead(self, dialog: Dialog) -> Lead:
        result = await self.session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        lead = result.scalar_one_or_none()
        if lead is not None:
            return lead
        lead = Lead(dialog_id=dialog.id, qualification_status="new", funnel_state="NEW_LEAD")
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
        return _collapse_outbound_duplicates(messages)

    async def _inbound_batch_since_last_outbound(self, dialog_id) -> list[Message]:
        from app.services.funnel_graph.turn_buffer import FunnelTurnBufferService

        return await FunnelTurnBufferService(self.session, debounce_seconds=0).inbound_batch_since_last_outbound(dialog_id)

    async def _reply_context_map(self, messages: list[Message]) -> dict[str, str]:
        """For messages that quote/reply to another, map message.id -> quoted body."""
        wanted = {m.reply_to_message_id for m in messages if getattr(m, "reply_to_message_id", None)}
        if not wanted:
            return {}
        rows = (
            await self.session.execute(
                select(Message.crmchat_message_id, Message.body).where(
                    Message.crmchat_message_id.in_(wanted)
                )
            )
        ).all()
        by_external = {ext: body for ext, body in rows if body}
        context: dict[str, str] = {}
        for m in messages:
            quoted = by_external.get(getattr(m, "reply_to_message_id", None))
            if quoted:
                context[str(m.id)] = quoted
        return context

    async def _has_newer_inbound(self, dialog_id, baseline: Message | None) -> bool:
        if baseline is None:
            return False
        threshold = message_activity_at(baseline)
        result = await self.session.execute(
            select(Message)
            .where(
                Message.dialog_id == dialog_id,
                Message.direction == "inbound",
                Message.id != baseline.id,
                func.coalesce(Message.sent_at, Message.created_at) > threshold,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def _mark_superseded_runtime(self, *, runtime: LeadFunnelRuntime, baseline_message: Message | None) -> None:
        metadata = dict(runtime.metadata_json or {})
        metadata["last_superseded_graph_run"] = {
            "reason": "newer_inbound_arrived_during_run",
            "baseline_message_id": str(baseline_message.id) if baseline_message is not None else None,
            "discarded_at": datetime.now(UTC).isoformat(),
        }
        runtime.metadata_json = metadata
        await self.session.flush()

    def _initial_state(
        self,
        *,
        lead: Lead,
        dialog: Dialog,
        runtime: LeadFunnelRuntime,
        recent_messages: list[Message],
        message_batch: list[Message],
        reply_context: dict[str, str] | None = None,
    ) -> FunnelGraphState:
        metadata = dict(runtime.metadata_json or {})
        metadata["telegram_username"] = dialog.telegram_username
        profile = dict(metadata.get("candidate_profile") or metadata.get("slots") or {})
        reply_context = reply_context or {}

        def _funnel_msg(item: Message) -> dict:
            data = message_to_funnel_message(item).model_dump()
            quoted = reply_context.get(str(item.id))
            if quoted:
                data["body"] = apply_reply_context(data.get("body") or "", quoted)
            return data

        return {
            "candidate_id": str(lead.id),
            "lead_id": str(lead.id),
            "dialog_id": str(dialog.id),
            "thread_id": runtime.thread_id,
            "stage": runtime.stage,
            "status": runtime.status,
            "current_goal": runtime.current_goal,
            "candidate_profile": profile,
            "slots": profile,
            "sent_voice_packs": list(metadata.get("sent_voice_packs") or []),
            "sent_templates": list(metadata.get("sent_templates") or []),
            "message_batch": [_funnel_msg(item) for item in message_batch],
            "recent_messages": [message_to_funnel_message(item).model_dump() for item in recent_messages],
            "retrieved_cards": [],
            "pending_actions": [],
            "metadata": metadata,
        }

    async def _persist_runtime(
        self,
        *,
        runtime: LeadFunnelRuntime,
        result: FunnelGraphState,
        message: Message | None,
    ) -> None:
        runtime.stage = str(result.get("stage") or runtime.stage or "new")
        runtime.status = str(result.get("status") or runtime.status or "active")
        runtime.current_goal = result.get("current_goal")
        metadata = dict(result.get("metadata") or {})
        metadata["candidate_profile"] = dict(result.get("candidate_profile") or result.get("slots") or {})
        metadata["slots"] = metadata["candidate_profile"]
        metadata["sent_voice_packs"] = list(result.get("sent_voice_packs") or [])
        metadata["sent_templates"] = list(result.get("sent_templates") or [])
        if result.get("reply_text"):
            metadata["last_reply_text"] = result.get("reply_text")
        if result.get("intent"):
            metadata["last_intent"] = result.get("intent")
        runtime.metadata_json = metadata
        if message is not None:
            runtime.last_processed_message_id = message.id
        await self.session.flush()


def is_bot_silenced(runtime: LeadFunnelRuntime) -> bool:
    """True when the dialog is human-controlled and the bot must not write.

    Triggered by a human handoff (``status == "handoff"`` / ``stage ==
    "human_handoff"``) or by an explicit manual pause flag in the runtime
    metadata (``bot_paused``). ``scripts/resume_funnel_dialog.py`` clears all of
    these to hand the conversation back to the bot.
    """
    if str(runtime.status or "") == "handoff":
        return True
    if str(runtime.stage or "") == "human_handoff":
        return True
    metadata = runtime.metadata_json or {}
    return bool(metadata.get("bot_paused"))


def _collapse_outbound_duplicates(messages: list[Message]) -> list[Message]:
    """Свернуть дубли исходящих, оставшиеся от readback-копий (sent + synced).

    Даже после фикса на стороне поллинга в БД могут лежать ранее накопленные пары
    «то же исходящее дважды». Если скормить их LLM, она решит, что написала дважды,
    и извинится. Поэтому соседние исходящие с одинаковым текстом схлопываем в одно.
    """
    collapsed: list[Message] = []
    for message in messages:
        if (
            message.direction == "outbound"
            and collapsed
            and collapsed[-1].direction == "outbound"
            and _norm_body(collapsed[-1].body) == _norm_body(message.body)
            and _norm_body(message.body) != ""
        ):
            continue
        collapsed.append(message)
    return collapsed


def _norm_body(body: str | None) -> str:
    return " ".join((body or "").split()).lower()


def apply_reply_context(body: str, quoted: str) -> str:
    """Fold a quoted (replied-to) message into the inbound text the funnel reads.

    A bare reply like "." or "👍" carries its meaning entirely in the quoted
    message, so we surface the quote as the effective text. A substantive reply
    keeps its body and gets the quote appended as context.
    """
    quoted = (quoted or "").strip()
    if not quoted:
        return body
    # Trivial reply = no letters/digits at all (bare ".", emoji reaction, etc.):
    # its meaning lives in the quote, so use the quote as the effective text.
    core = re.sub(r"\W+", "", body or "", flags=re.UNICODE)
    if not core:
        return quoted
    return f"{body} (в ответ на: «{quoted}»)"


def message_to_funnel_message(message: Message) -> FunnelMessage:
    return FunnelMessage(
        id=str(message.id),
        direction=message.direction,
        sender_type=message.sender_type,
        body=message.body,
        sent_at=message.sent_at.isoformat() if message.sent_at else None,
    )


def latest_message_from_batch(messages: list[Message]) -> Message | None:
    inbound = [message for message in messages if message.direction == "inbound"]
    if not inbound:
        return None
    return max(inbound, key=message_activity_at)


def mark_superseded_result(result: FunnelGraphState, *, baseline_message: Message | None) -> FunnelGraphState:
    metadata = dict(result.get("metadata") or {})
    metadata["run_superseded"] = True
    metadata["superseded_reason"] = "newer_inbound_arrived_during_run"
    metadata["superseded_baseline_message_id"] = str(baseline_message.id) if baseline_message is not None else None
    agent_run = dict(result.get("agent_run") or {})
    if agent_run:
        agent_run["superseded"] = True
        agent_run["superseded_reason"] = metadata["superseded_reason"]
    return {
        **result,
        "send_reply": False,
        "outgoing_messages": [],
        "pending_actions": [],
        "metadata": metadata,
        "agent_run": agent_run,
    }


def parse_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def actions_from_graph_result(result: FunnelGraphState) -> list[dict[str, Any]]:
    actions = list(result.get("pending_actions") or [])
    next_action = result.get("next_action")
    if next_action:
        actions.append(dict(next_action))
    return actions
