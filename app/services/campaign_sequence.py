from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog
from app.models.campaign import CampaignStep
from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.human_handoff import HumanHandoff
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.repositories.campaign import CampaignStepRepository
from app.repositories.dialog_sequence_run import DialogSequenceRunRepository
from app.repositories.lead import LeadRepository
from app.repositories.outbound_job import OutboundJobRepository
from app.services.crmchat_diagnostics import redact_value
from app.services.llm_adapter import LLMAdapter, LLMDecision


class CampaignSequenceService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        llm_adapter: LLMAdapter | None = None,
    ) -> None:
        self.session = session
        self.llm_adapter = llm_adapter

    async def start(self, run: DialogSequenceRun) -> DialogSequenceRun:
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        run.status = "active"
        await self.process_next_steps(run)
        return run

    async def handle_inbound_message(
        self, *, dialog_id: str, message_id: str | None = None
    ) -> DialogSequenceRun | None:
        run = await DialogSequenceRunRepository(self.session).get_active_by_dialog(dialog_id)
        if run is None or run.status != "awaiting_reply":
            return run
        run.last_inbound_message_id = message_id
        run.current_step_position = run.awaiting_reply_after_step or run.current_step_position
        run.awaiting_reply_after_step = None
        run.status = "active"
        await self.process_next_steps(run)
        return run

    async def mark_outbound_sent(self, job: OutboundJob) -> None:
        if not job.sequence_run_id or not job.campaign_step_id:
            return
        run = await self.session.get(DialogSequenceRun, job.sequence_run_id)
        step = await self.session.get(CampaignStep, job.campaign_step_id)
        if run is None or step is None or not step.wait_for_reply:
            return
        if run.awaiting_reply_after_step != step.position:
            return
        if run.status in {"active", "waiting_outbound"}:
            run.status = "awaiting_reply"
            await self._audit(
                run.dialog_id,
                "sequence.await_reply",
                {"sequence_run_id": str(run.id), "step_position": step.position},
                status="awaiting_reply",
            )

    async def process_next_steps(self, run: DialogSequenceRun) -> None:
        if run.status in {"completed", "failed", "handoff"}:
            return
        steps = await CampaignStepRepository(self.session).list_by_campaign(run.campaign_id)
        next_steps = [step for step in steps if step.position > run.current_step_position]
        now = datetime.now(UTC)
        for step in next_steps:
            if step.step_type == "fixed_message":
                await self._schedule_fixed_message(run, step, now=now)
                if step.wait_for_reply:
                    run.status = "waiting_outbound"
                    run.awaiting_reply_after_step = step.position
                    return
                continue
            if step.step_type == "llm_decision":
                await self._run_llm_step(run, step)
                return
            raise ValueError(f"Unsupported campaign step type: {step.step_type}")
        run.status = "completed"
        run.completed_at = datetime.now(UTC)

    async def _schedule_fixed_message(
        self, run: DialogSequenceRun, step: CampaignStep, *, now: datetime
    ) -> None:
        dialog = await self.session.get(Dialog, run.dialog_id)
        if dialog is None:
            raise ValueError(f"Dialog not found: {run.dialog_id}")
        if not step.message_text:
            raise ValueError(f"Campaign step {step.id} has no message text")
        existing_job = await OutboundJobRepository(self.session).get_by_sequence_step(
            sequence_run_id=run.id,
            campaign_step_id=step.id,
        )
        if existing_job:
            run.current_step_position = step.position
            if step.wait_for_reply and run.status != "awaiting_reply":
                run.status = "waiting_outbound"
                run.awaiting_reply_after_step = step.position
            return

        scheduled_at = now + timedelta(seconds=step.delay_seconds)
        message = Message(
            dialog_id=dialog.id,
            direction="outbound",
            sender_type="agent",
            body=step.message_text,
            status="scheduled",
        )
        self.session.add(message)
        await self.session.flush()

        job = OutboundJob(
            account_id=dialog.account_id,
            dialog_id=dialog.id,
            message_id=message.id,
            campaign_id=run.campaign_id,
            sequence_run_id=run.id,
            campaign_step_id=step.id,
            target_username=dialog.telegram_username,
            peer=build_peer_from_dialog(dialog),
            text=step.message_text,
            status="queued",
            scheduled_at=scheduled_at,
            next_attempt_at=scheduled_at,
        )
        self.session.add(job)
        run.current_step_position = step.position
        await self._audit(
            dialog.id,
            "sequence.schedule_fixed_message",
            {
                "sequence_run_id": str(run.id),
                "step_position": step.position,
                "outbound_job_text": step.message_text,
                "scheduled_at": scheduled_at.isoformat(),
            },
            status="queued",
        )
        await self.session.flush()

    async def _run_llm_step(self, run: DialogSequenceRun, step: CampaignStep) -> None:
        dialog = await self.session.get(Dialog, run.dialog_id)
        if dialog is None:
            raise ValueError(f"Dialog not found: {run.dialog_id}")
        run.status = "awaiting_llm"
        run.current_step_position = step.position
        decision = await self._decide(dialog)
        run.llm_decision_json = decision.model_dump()

        lead = await LeadRepository(self.session).get_by_dialog(dialog.id)
        if lead is None:
            lead = Lead(dialog_id=dialog.id, qualification_status=decision.lead_status)
            self.session.add(lead)
        else:
            lead.qualification_status = decision.lead_status
        lead.summary = decision.reply_text or decision.handoff_reason

        if decision.decision == "handoff":
            self.session.add(
                HumanHandoff(
                    dialog_id=dialog.id,
                    reason=decision.handoff_reason or "LLM requested human handoff",
                    status="open",
                )
            )
            run.status = "handoff"
        elif decision.decision == "reply" and decision.reply_text:
            await self._schedule_llm_reply(run, dialog, decision.reply_text)
            run.status = "completed"
        else:
            run.status = "completed"

        run.completed_at = datetime.now(UTC)
        await self._audit(
            dialog.id,
            "sequence.llm_decision",
            {"decision": redact_value(decision.model_dump())},
            status=decision.decision,
        )
        await self.session.flush()

    async def _decide(self, dialog: Dialog) -> LLMDecision:
        from app.services.dialog_state import DialogStateService

        snapshot = await DialogStateService(self.session).get_snapshot(str(dialog.id), message_limit=30)
        lead_context = {
            "dialog_status": snapshot.dialog_status,
            "lead_status": snapshot.lead_status,
            "telegram_username": dialog.telegram_username,
        }
        if self.llm_adapter is not None:
            return await self.llm_adapter.decide_next_action(
                dialog_messages=snapshot.messages,
                lead_context=lead_context,
            )
        async with LLMAdapter() as adapter:
            return await adapter.decide_next_action(
                dialog_messages=snapshot.messages,
                lead_context=lead_context,
            )

    async def _schedule_llm_reply(
        self, run: DialogSequenceRun, dialog: Dialog, text: str
    ) -> None:
        now = datetime.now(UTC)
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
                campaign_id=run.campaign_id,
                sequence_run_id=run.id,
                target_username=dialog.telegram_username,
                peer=build_peer_from_dialog(dialog),
                text=text,
                status="queued",
                scheduled_at=now,
                next_attempt_at=now,
            )
        )

    async def recover_stale_runs(self, *, older_than_seconds: int = 900) -> dict[str, int]:
        repository = DialogSequenceRunRepository(self.session)
        waiting_outbound = await repository.list_stale_waiting_outbound(
            older_than_seconds=older_than_seconds
        )
        awaiting_llm = await repository.list_stale_awaiting_llm(
            older_than_seconds=older_than_seconds
        )
        recovered = failed = 0
        for run in waiting_outbound:
            await self.process_next_steps(run)
            recovered += 1
        for run in awaiting_llm:
            try:
                await self.process_next_steps(run)
                recovered += 1
            except Exception as exc:
                run.status = "failed"
                run.error_message = str(exc)
                failed += 1
        await self.session.commit()
        return {"recovered": recovered, "failed": failed}

    async def _audit(self, dialog_id, action_type: str, payload: dict[str, Any], *, status: str) -> None:
        self.session.add(
            AgentActionLog(
                dialog_id=dialog_id,
                action_type=action_type,
                payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                status=status,
            )
        )
        await self.session.flush()


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
