from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.human_handoff import HumanHandoff
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.knowledge import repair_mojibake
from app.services.funnel_graph.state import FunnelAction
from app.services.lead_notifier import LeadNotifier


class FunnelActionExecutor:
    def __init__(self, session: AsyncSession, *, notifier: LeadNotifier | None = None) -> None:
        self.session = session
        self.notifier = notifier or LeadNotifier()

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
            elif action.type == "schedule_birthday_followup":
                await self._schedule_birthday_followup(dialog=dialog, action=action)

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
        body = repair_mojibake(action.caption or "голосовое сообщение")
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

    async def _schedule_birthday_followup(self, *, dialog: Dialog, action: FunnelAction) -> None:
        """Запланировать два отложенных сообщения к 18-летию: поздравление в сам
        день рождения и сообщение о работе на следующий день. Джобы помечаются
        `scheduled_followup`, чтобы их НЕ отменяли новые входящие до даты."""
        if not action.birthday_at:
            return
        try:
            birthday_at = datetime.fromisoformat(str(action.birthday_at))
        except ValueError:
            return
        if birthday_at.tzinfo is None:
            birthday_at = birthday_at.replace(tzinfo=UTC)
        base_key = action.idempotency_key or f"birthday18:{dialog.id}"
        await self._enqueue_scheduled(
            dialog, repair_mojibake(action.text or ""), birthday_at, f"{base_key}:congrats"
        )
        await self._enqueue_scheduled(
            dialog, repair_mojibake(action.caption or ""), birthday_at + timedelta(days=1), f"{base_key}:work"
        )

    async def _enqueue_scheduled(
        self, dialog: Dialog, text: str, scheduled_at: datetime, idempotency_key: str
    ) -> None:
        if not text or await self._existing_job(dialog.id, idempotency_key):
            return
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
                media_metadata={
                    "source": "langgraph_funnel",
                    "funnel_idempotency_key": idempotency_key,
                    "scheduled_followup": True,
                },
            )
        )
        await self.session.flush()

    async def _handoff(self, *, runtime: LeadFunnelRuntime, dialog: Dialog, reason: str | None) -> None:
        runtime.stage = "human_handoff"
        runtime.status = "handoff"
        # Идемпотентность: хендофф-узел может отработать повторно (терминальная
        # стадия, переобработка). Запись и алерт создаём только один раз на диалог.
        if await self._existing_handoff(dialog.id):
            await self.session.flush()
            return
        handoff_reason = reason or "LangGraph funnel requested human handoff"
        self.session.add(
            HumanHandoff(
                dialog_id=dialog.id,
                reason=handoff_reason,
                status="open",
            )
        )
        await self.session.flush()
        await self._notify_handoff(runtime=runtime, dialog=dialog, reason=handoff_reason)

    async def _notify_handoff(self, *, runtime: LeadFunnelRuntime, dialog: Dialog, reason: str) -> None:
        if not self.notifier.handoff_enabled:
            return
        metadata = dict(runtime.metadata_json or {})
        profile = dict(metadata.get("candidate_profile") or metadata.get("slots") or {})
        account_label: str | None = None
        if dialog.account_id is not None:
            account = await self.session.get(Account, dialog.account_id)
            if account is not None:
                account_label = account.display_name or account.telegram_username or account.crmchat_account_id
        await self.notifier.notify_handoff(
            telegram_username=dialog.telegram_username,
            account_label=account_label,
            profile=profile,
            reason=reason,
        )

    async def _existing_handoff(self, dialog_id) -> HumanHandoff | None:
        result = await self.session.execute(
            select(HumanHandoff).where(HumanHandoff.dialog_id == dialog_id).limit(1)
        )
        return result.scalar_one_or_none()

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
