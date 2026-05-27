from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.brain_v2 import BrainRun, LeadBrainState
from app.models.dialog import Dialog
from app.models.human_handoff import HumanHandoff
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.brain_v2.schemas import ExecutorAction
from app.services.campaign_sequence import build_peer_from_dialog


class BrainExecutor:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def execute_or_shadow(
        self,
        *,
        brain_run: BrainRun,
        dialog: Dialog,
        action: ExecutorAction,
        shadow_mode: bool,
    ) -> None:
        if shadow_mode:
            brain_run.status = "shadow_pending"
            await self.session.flush()
            return
        await self._execute(brain_run=brain_run, dialog=dialog, action=action)

    async def approve(self, brain_run: BrainRun) -> BrainRun:
        if brain_run.status not in {"shadow_pending", "rejected"}:
            return brain_run
        dialog = await self.session.get(Dialog, brain_run.dialog_id)
        if dialog is None:
            brain_run.status = "failed"
            brain_run.error_message = f"Dialog not found: {brain_run.dialog_id}"
            await self.session.flush()
            return brain_run
        action = ExecutorAction.model_validate(brain_run.executor_action or {"type": "do_nothing"})
        await self._execute(brain_run=brain_run, dialog=dialog, action=action)
        brain_run.approved_at = datetime.now(UTC)
        await self.session.flush()
        return brain_run

    async def reject(self, brain_run: BrainRun, *, reason: str | None = None) -> BrainRun:
        brain_run.status = "rejected"
        brain_run.rejected_at = datetime.now(UTC)
        if reason:
            metadata = dict(brain_run.metadata_json or {})
            metadata["reject_reason"] = reason
            brain_run.metadata_json = metadata
        await self.session.flush()
        return brain_run

    async def _execute(self, *, brain_run: BrainRun, dialog: Dialog, action: ExecutorAction) -> None:
        if action.type in {"send_message", "schedule_followup"}:
            await self._enqueue_message(brain_run=brain_run, dialog=dialog, action=action)
            brain_run.status = "approved" if brain_run.shadow_mode else "executed"
            return
        if action.type == "handoff":
            self.session.add(
                HumanHandoff(
                    dialog_id=dialog.id,
                    reason=action.handoff_reason or "Brain V2 requested human handoff",
                    status="open",
                )
            )
            await self._close_or_stage(brain_run, stage="handoff")
            brain_run.status = "approved" if brain_run.shadow_mode else "executed"
            return
        if action.type == "close_lost":
            await self._close_or_stage(brain_run, stage="closed")
            brain_run.status = "approved" if brain_run.shadow_mode else "executed"
            return
        brain_run.status = "approved" if brain_run.shadow_mode else "executed"

    async def _enqueue_message(self, *, brain_run: BrainRun, dialog: Dialog, action: ExecutorAction) -> None:
        if not action.text:
            brain_run.status = "failed"
            brain_run.error_message = "Executor action has no text"
            return
        now = datetime.now(UTC)
        delay = action.followup_seconds
        delay = delay or 0
        scheduled_at = now + timedelta(seconds=max(0, delay))
        message = Message(
            dialog_id=dialog.id,
            direction="outbound",
            sender_type="agent",
            body=action.text,
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
                text=action.text,
                status="queued",
                scheduled_at=scheduled_at,
                next_attempt_at=scheduled_at,
            )
        )
        await self.session.flush()

    async def _close_or_stage(self, brain_run: BrainRun, *, stage: str) -> None:
        result = await self.session.execute(
            select(LeadBrainState).where(LeadBrainState.lead_id == brain_run.lead_id).limit(1)
        )
        state = result.scalar_one_or_none()
        if state is not None:
            state.stage = stage
            state.status = "closed" if stage == "closed" else "active"
        brain_run.stage_after = stage
        await self.session.flush()
