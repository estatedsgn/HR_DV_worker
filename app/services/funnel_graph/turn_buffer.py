from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob


@dataclass(slots=True, frozen=True)
class FunnelTurnBufferResult:
    ready: bool
    retry_at: datetime | None
    reason: str | None
    latest_message: Message | None
    message_batch: list[Message]


class FunnelTurnBufferService:
    def __init__(self, session: AsyncSession, *, debounce_seconds: int = 10) -> None:
        self.session = session
        self.debounce_seconds = max(0, debounce_seconds)

    async def prepare_inbound_turn(
        self,
        *,
        dialog: Dialog,
        lead: Lead | None,
        runtime: LeadFunnelRuntime | None,
        message_id: str | None,
    ) -> FunnelTurnBufferResult:
        latest = await self.latest_inbound(dialog.id)
        if latest is None:
            return FunnelTurnBufferResult(True, None, None, None, [])
        if message_id and str(latest.id) != str(message_id):
            return FunnelTurnBufferResult(False, None, "covered_by_newer_inbound", latest, [])
        if runtime is not None and runtime.last_processed_message_id and str(runtime.last_processed_message_id) == str(latest.id):
            return FunnelTurnBufferResult(False, None, "already_processed_latest_inbound", latest, [])
        if runtime is not None and is_before_reset_baseline(runtime, latest):
            return FunnelTurnBufferResult(False, None, "inbound_before_reset_baseline", latest, [])

        await self.cancel_pending_outbound(dialog.id, reason="new inbound before langgraph reply", message_id=str(latest.id))
        if runtime is not None:
            activity_at = message_activity_at(latest)
            runtime.last_candidate_activity_at = activity_at
            runtime.debounce_until = activity_at + timedelta(seconds=self.debounce_seconds)
            retry_at = self._retry_at_for_runtime(runtime)
            if retry_at is not None:
                return FunnelTurnBufferResult(False, retry_at, "waiting_for_candidate_quiet_window", latest, [])
        batch = await self.inbound_batch_since_last_outbound(dialog.id)
        return FunnelTurnBufferResult(True, None, None, latest, batch)

    async def record_typing_activity(
        self,
        *,
        dialog: Dialog,
        runtime: LeadFunnelRuntime | None,
        payload: dict[str, Any],
    ) -> None:
        await self.cancel_pending_outbound(dialog.id, reason="candidate typing before langgraph reply")
        if runtime is None:
            return
        now = datetime.now(UTC)
        runtime.last_candidate_activity_at = now
        runtime.last_candidate_typing_at = parse_event_datetime(payload) or now
        runtime.debounce_until = now + timedelta(seconds=self.debounce_seconds)
        await self.session.flush()

    async def inbound_batch_since_last_outbound(self, dialog_id) -> list[Message]:
        last_outbound = await self._last_outbound(dialog_id)
        query = select(Message).where(Message.dialog_id == dialog_id, Message.direction == "inbound")
        if last_outbound is not None:
            threshold = message_activity_at(last_outbound)
            query = query.where(func.coalesce(Message.sent_at, Message.created_at) > threshold)
        result = await self.session.execute(
            query.order_by(Message.sent_at.asc(), Message.created_at.asc()).limit(20)
        )
        return list(result.scalars().all())

    async def latest_inbound(self, dialog_id) -> Message | None:
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id, Message.direction == "inbound")
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def cancel_pending_outbound(self, dialog_id, *, reason: str, message_id: str | None = None) -> int:
        result = await self.session.execute(
            select(OutboundJob).where(
                OutboundJob.dialog_id == dialog_id,
                OutboundJob.status.in_(["queued", "retry"]),
                # Never break an in-flight voice pack: let the current pack finish,
                # we react to the candidate's message on the next turn.
                OutboundJob.job_type != "voice",
            )
        )
        cancelled = 0
        for job in result.scalars().all():
            job.status = "cancelled"
            job.next_attempt_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.error_message = f"{reason}; inbound_message_id={message_id}" if message_id else reason
            if job.message_id:
                message = await self.session.get(Message, job.message_id)
                if message is not None:
                    message.status = "cancelled"
            cancelled += 1
        await self.session.flush()
        return cancelled

    async def _last_outbound(self, dialog_id) -> Message | None:
        result = await self.session.execute(
            select(Message)
            .where(
                Message.dialog_id == dialog_id,
                Message.direction == "outbound",
                Message.status.in_(["sent", "delivered", "read"]),
            )
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def _retry_at_for_runtime(self, runtime: LeadFunnelRuntime) -> datetime | None:
        now = datetime.now(UTC)
        candidates = [
            value
            for value in (runtime.debounce_until, runtime.last_candidate_typing_at)
            if value is not None
        ]
        if runtime.last_candidate_typing_at is not None:
            candidates.append(runtime.last_candidate_typing_at + timedelta(seconds=self.debounce_seconds))
        retry_at = max(candidates) if candidates else None
        if retry_at is not None and retry_at > now:
            return retry_at
        return None


def message_activity_at(message: Message) -> datetime:
    value = message.sent_at or message.created_at or datetime.now(UTC)
    return ensure_aware(value)


def parse_event_datetime(payload: dict[str, Any]) -> datetime | None:
    for key in ("sent_at", "date", "timestamp", "created_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            return ensure_aware(value)
        if isinstance(value, str) and value:
            try:
                return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
    return None


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_before_reset_baseline(runtime: LeadFunnelRuntime, message: Message) -> bool:
    baseline = reset_baseline_at(runtime)
    if baseline is None or message.sent_at is None:
        return False
    return ensure_aware(message.sent_at) <= baseline


def reset_baseline_at(runtime: LeadFunnelRuntime) -> datetime | None:
    metadata = runtime.metadata_json or {}
    for key in ("restart_at", "reset_at"):
        value = metadata.get(key)
        if isinstance(value, datetime):
            return ensure_aware(value)
        if isinstance(value, str) and value:
            try:
                return ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
            except ValueError:
                continue
    return None


def is_candidate_typing_event(payload: dict[str, Any]) -> bool:
    event_type = str(
        payload.get("event_type")
        or payload.get("eventType")
        or payload.get("type")
        or payload.get("_")
        or ""
    ).lower()
    action = payload.get("action")
    if isinstance(action, dict):
        action = action.get("_") or action.get("type")
    action_text = str(action or payload.get("status") or "").lower()
    return any(marker in event_type for marker in ("typing", "user_typing", "updateuserstatus")) or any(
        marker in action_text for marker in ("typing", "sendmessagetypingaction")
    )
