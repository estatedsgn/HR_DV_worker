from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog
from app.models.dialog import Dialog
from app.models.human_handoff import HumanHandoff
from app.repositories.lead import LeadRepository
from app.repositories.log import LogRepository
from app.repositories.message import MessageRepository


@dataclass(slots=True, frozen=True)
class DialogStateSnapshot:
    dialog_id: str
    dialog_status: str
    lead_status: str
    active_handoff_status: str | None
    last_agent_action: str | None
    messages: list[dict[str, Any]]


class DialogStateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.message_repository = MessageRepository(session)
        self.lead_repository = LeadRepository(session)
        self.log_repository = LogRepository(session)

    async def get_snapshot(self, dialog_id: str, *, message_limit: int = 20) -> DialogStateSnapshot:
        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            return DialogStateSnapshot(
                dialog_id=str(dialog_id),
                dialog_status="unknown",
                lead_status="unknown",
                active_handoff_status=None,
                last_agent_action=None,
                messages=[],
            )

        messages = await self.message_repository.list_by_dialog(dialog.id, limit=message_limit)
        lead = await self.lead_repository.get_by_dialog(dialog.id)
        active_handoff = await self._get_active_handoff(dialog.id)
        last_action = await self._get_last_agent_action(dialog.id)

        normalized_messages = [
            {
                "id": str(message.id),
                "direction": message.direction,
                "sender_type": message.sender_type,
                "body": message.body,
                "status": message.status,
                "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            }
            for message in messages
            if isinstance(message.body, str)
        ]

        return DialogStateSnapshot(
            dialog_id=str(dialog.id),
            dialog_status=dialog.status or "unknown",
            lead_status=(lead.qualification_status if lead else "unknown"),
            active_handoff_status=(active_handoff.status if active_handoff else None),
            last_agent_action=(last_action.action_type if last_action else None),
            messages=normalized_messages,
        )

    async def update_dialog_status(
        self,
        dialog_id: str,
        *,
        new_status: str,
        reason: str | None = None,
        actor: str = "system",
    ) -> DialogStateSnapshot:
        allowed = {"open", "pending_review", "in_progress", "closed", "ignored", "metadata_only"}
        if new_status not in allowed:
            raise ValueError(f"Unsupported dialog status transition target: {new_status}")

        dialog = await self.session.get(Dialog, dialog_id)
        if dialog is None:
            raise ValueError(f"Dialog not found: {dialog_id}")

        previous_status = dialog.status
        dialog.status = new_status
        await self.session.flush()

        payload = json.dumps(
            {
                "transition": {"from": previous_status, "to": new_status},
                "reason": reason,
                "actor": actor,
            }
        )
        action = AgentActionLog(
            dialog_id=dialog.id,
            action_type="state_transition",
            reasoning=reason,
            payload_json=payload,
            status="logged",
        )
        await self.log_repository.agent_actions.add(action)
        await self.session.commit()

        return await self.get_snapshot(str(dialog.id))

    async def _get_active_handoff(self, dialog_id: str) -> HumanHandoff | None:
        result = await self.session.execute(
            select(HumanHandoff)
            .where(HumanHandoff.dialog_id == dialog_id, HumanHandoff.status.in_(["open", "pending"]))
            .order_by(HumanHandoff.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_last_agent_action(self, dialog_id: str) -> AgentActionLog | None:
        result = await self.session.execute(
            select(AgentActionLog)
            .where(AgentActionLog.dialog_id == dialog_id)
            .order_by(AgentActionLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
