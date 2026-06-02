from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.human_handoff import HumanHandoff
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.knowledge import repair_mojibake
from app.services.funnel_graph.state import FunnelAction


class FunnelActionExecutor:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(self, *, dialog: Dialog, lead: Lead, runtime: LeadFunnelRuntime, actions: list[dict[str, Any]]) -> None:
        for raw in actions:
            action = FunnelAction.model_validate(raw)
            if action.type == "send_text":
                await self._send_text(dialog=dialog, action=action)
            elif action.type == "send_voice":
                await self._send_voice(dialog=dialog, action=action)
            elif action.type == "handoff":
                await self._handoff(runtime=runtime, dialog=dialog, reason=action.reason)
            elif action.type == "close_lost":
                await self._close_lost(runtime=runtime, lead=lead, reason=action.reason)
            elif action.type == "do_not_contact":
                await self._do_not_contact(runtime=runtime, lead=lead, reason=action.reason)

    async def _send_text(self, *, dialog: Dialog, action: FunnelAction) -> None:
        if not action.text:
            return
        text = repair_mojibake(action.text)
        if action.idempotency_key and await self._existing_job(dialog.id, action.idempotency_key):
            return
        scheduled_at = datetime.now(UTC) + timedelta(seconds=max(0, int(action.delay_seconds or 0)))
        message = Message(
            dialog_id=dialog.id,
            direction="outbound",
            sender_type="agent",
            body=text,
            status="scheduled",
        )
        self.session.add(message)
        await self.session.flush()
        self.session.add(
            OutboundJob(
                account_id=dialog.account_id,
                dialog_id=dialog.id,
                message_id=message.id,
                target_username=dialog.telegram_username,
                peer=build_peer_from_dialog(dialog),
                text=text,
                status="queued",
                scheduled_at=scheduled_at,
                next_attempt_at=scheduled_at,
                media_metadata=metadata_for_action(action),
            )
        )
        await self.session.flush()

    async def _send_voice(self, *, dialog: Dialog, action: FunnelAction) -> None:
        if not action.media_path:
            return
        if action.idempotency_key and await self._existing_job(dialog.id, action.idempotency_key):
            return
        media_path = str(Path(action.media_path))
        body = repair_mojibake(action.caption or f"[voice] {Path(media_path).name}")
        scheduled_at = datetime.now(UTC) + timedelta(seconds=max(0, int(action.delay_seconds or 0)))
        message = Message(
            dialog_id=dialog.id,
            direction="outbound",
            sender_type="agent",
            body=body,
            status="scheduled",
        )
        self.session.add(message)
        await self.session.flush()
        self.session.add(
            OutboundJob(
                account_id=dialog.account_id,
                dialog_id=dialog.id,
                message_id=message.id,
                target_username=dialog.telegram_username,
                peer=build_peer_from_dialog(dialog),
                job_type="voice",
                text=body,
                media_path=media_path,
                media_mime_type="audio/ogg",
                media_metadata=metadata_for_action(action),
                typing_action="sendMessageRecordAudioAction",
                status="queued",
                scheduled_at=scheduled_at,
                next_attempt_at=scheduled_at,
            )
        )
        await self.session.flush()

    async def _handoff(self, *, runtime: LeadFunnelRuntime, dialog: Dialog, reason: str | None) -> None:
        runtime.stage = "human_handoff"
        runtime.status = "handoff"
        self.session.add(
            HumanHandoff(
                dialog_id=dialog.id,
                reason=reason or "LangGraph funnel requested human handoff",
                status="open",
            )
        )
        await self.session.flush()

    async def _close_lost(self, *, runtime: LeadFunnelRuntime, lead: Lead, reason: str | None) -> None:
        runtime.stage = "lost"
        runtime.status = "closed"
        lead.qualification_status = "not_qualified"
        lead.lost_reason = reason or "LangGraph funnel closed lead"
        await self.session.flush()

    async def _do_not_contact(self, *, runtime: LeadFunnelRuntime, lead: Lead, reason: str | None) -> None:
        runtime.stage = "do_not_contact"
        runtime.status = "closed"
        lead.qualification_status = "not_qualified"
        lead.lost_reason = reason or "Candidate asked not to contact"
        await self.session.flush()

    async def _existing_job(self, dialog_id, idempotency_key: str) -> OutboundJob | None:
        result = await self.session.execute(
            select(OutboundJob)
            .where(
                OutboundJob.dialog_id == dialog_id,
                OutboundJob.media_metadata.op("->>")("funnel_idempotency_key") == idempotency_key,
                OutboundJob.status.in_(["queued", "retry", "processing", "sent"]),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


def metadata_for_action(action: FunnelAction) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source": "langgraph_funnel"}
    if action.idempotency_key:
        metadata["funnel_idempotency_key"] = action.idempotency_key
    if action.reply_group_id:
        metadata["reply_group_id"] = action.reply_group_id
    if action.reply_group_index is not None:
        metadata["reply_group_index"] = action.reply_group_index
    if action.reply_group_size is not None:
        metadata["reply_group_size"] = action.reply_group_size
    if action.recording_delay_seconds is not None:
        metadata["recording_delay_seconds"] = action.recording_delay_seconds
    if action.duration_seconds is not None:
        metadata["duration_seconds"] = action.duration_seconds
    if action.typing_delay_min_seconds is not None:
        metadata["typing_delay_min_seconds"] = action.typing_delay_min_seconds
    if action.typing_delay_max_seconds is not None:
        metadata["typing_delay_max_seconds"] = action.typing_delay_max_seconds
    if action.caption:
        metadata["caption"] = action.caption
    return metadata


def build_peer_from_dialog(dialog: Dialog) -> dict[str, Any] | None:
    if not dialog.telegram_peer_type or not dialog.telegram_peer_id:
        return None
    if dialog.telegram_peer_type == "user" and dialog.telegram_access_hash:
        return {
            "_": "inputPeerUser",
            "userId": int_or_str(dialog.telegram_peer_id),
            "accessHash": dialog.telegram_access_hash,
        }
    if dialog.telegram_peer_type == "channel" and dialog.telegram_access_hash:
        return {
            "_": "inputPeerChannel",
            "channelId": int_or_str(dialog.telegram_peer_id),
            "accessHash": dialog.telegram_access_hash,
        }
    if dialog.telegram_peer_type == "chat":
        return {"_": "inputPeerChat", "chatId": int_or_str(dialog.telegram_peer_id)}
    return None


def int_or_str(value: str) -> int | str:
    return int(value) if value.isdigit() else value
