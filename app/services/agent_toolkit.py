from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog
from app.models.human_handoff import HumanHandoff
from app.services.agent_policy import AgentPolicyService
from app.services.dialog_state import DialogStateService


class ReadDialogContextInput(BaseModel):
    dialog_id: str
    message_limit: int = Field(default=20, ge=1, le=100)


class ReadDialogContextOutput(BaseModel):
    dialog_id: str
    dialog_status: str
    lead_status: str
    active_handoff_status: str | None
    last_agent_action: str | None
    messages: list[dict]


class ProposeReplyInput(BaseModel):
    dialog_id: str
    strategy: str = Field(default="default", min_length=1, max_length=50)


class ProposeReplyOutput(BaseModel):
    dialog_id: str
    strategy: str
    proposed_reply: str
    safe: bool = True


class RequestHumanHandoffInput(BaseModel):
    dialog_id: str
    reason: str = Field(min_length=3, max_length=255)


class RequestHumanHandoffOutput(BaseModel):
    dialog_id: str
    handoff_id: str
    status: str
    idempotent: bool


@dataclass(slots=True)
class AgentToolkitService:
    session: AsyncSession

    async def read_dialog_context(
        self, payload: ReadDialogContextInput
    ) -> ReadDialogContextOutput:
        state = await DialogStateService(self.session).get_snapshot(
            payload.dialog_id, message_limit=payload.message_limit
        )
        await self._audit(
            payload.dialog_id,
            "tool.read_dialog_context",
            {"message_limit": payload.message_limit},
            status="completed",
        )
        await self.session.commit()
        return ReadDialogContextOutput(
            dialog_id=state.dialog_id,
            dialog_status=state.dialog_status,
            lead_status=state.lead_status,
            active_handoff_status=state.active_handoff_status,
            last_agent_action=state.last_agent_action,
            messages=state.messages,
        )

    async def propose_reply(self, payload: ProposeReplyInput) -> ProposeReplyOutput:
        context = await DialogStateService(self.session).get_snapshot(payload.dialog_id)
        text = self._build_reply(context.dialog_status, context.lead_status, payload.strategy)
        decision = await AgentPolicyService(self.session).evaluate_reply(context)
        safe = decision.decision == "allow"
        if decision.decision == "deny":
            text = ""
        output = ProposeReplyOutput(
            dialog_id=payload.dialog_id,
            strategy=payload.strategy,
            proposed_reply=text,
            safe=safe,
        )
        await self._audit(
            payload.dialog_id,
            "tool.propose_reply",
            {
                **output.model_dump(),
                "policy_decision": decision.decision,
                "policy_reason_code": decision.reason_code,
            },
            status=decision.decision,
        )
        await self.session.commit()
        return output

    async def request_human_handoff(
        self, payload: RequestHumanHandoffInput
    ) -> RequestHumanHandoffOutput:
        existing = await self._find_open_handoff(payload.dialog_id, payload.reason)
        if existing:
            result = RequestHumanHandoffOutput(
                dialog_id=payload.dialog_id,
                handoff_id=str(existing.id),
                status=existing.status,
                idempotent=True,
            )
            await self._audit(
                payload.dialog_id,
                "tool.request_human_handoff",
                result.model_dump(),
                status="idempotent",
            )
            await self.session.commit()
            return result

        handoff = HumanHandoff(dialog_id=payload.dialog_id, reason=payload.reason, status="open")
        self.session.add(handoff)
        await self.session.flush()
        result = RequestHumanHandoffOutput(
            dialog_id=payload.dialog_id,
            handoff_id=str(handoff.id),
            status=handoff.status,
            idempotent=False,
        )
        await self._audit(
            payload.dialog_id,
            "tool.request_human_handoff",
            result.model_dump(),
            status="completed",
        )
        await self.session.commit()
        return result

    async def _find_open_handoff(self, dialog_id: str, reason: str) -> HumanHandoff | None:
        result = await self.session.execute(
            select(HumanHandoff)
            .where(
                HumanHandoff.dialog_id == dialog_id,
                HumanHandoff.reason == reason,
                HumanHandoff.status.in_(["open", "pending"]),
            )
            .order_by(HumanHandoff.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _audit(self, dialog_id: str, action_type: str, payload: dict, *, status: str) -> None:
        log = AgentActionLog(
            dialog_id=dialog_id,
            action_type=action_type,
            payload_json=json.dumps(payload, default=str),
            status=status,
        )
        self.session.add(log)
        await self.session.flush()

    def _build_reply(self, dialog_status: str, lead_status: str, strategy: str) -> str:
        if strategy == "clarify":
            return "Спасибо! Уточните, пожалуйста, какой формат сотрудничества вам интересен?"
        if lead_status == "qualified":
            return "Отлично, вижу ваш интерес. Предлагаю перейти к следующему шагу и обсудить детали."
        if dialog_status in {"pending_review", "unknown"}:
            return "Спасибо за сообщение! Мы изучим ваш запрос и скоро ответим."
        return "Спасибо! Расскажите чуть подробнее о вашей задаче, чтобы я помог точнее."
