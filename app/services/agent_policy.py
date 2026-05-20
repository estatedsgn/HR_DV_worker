from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_action_log import AgentActionLog
from app.services.dialog_state import DialogStateSnapshot


class PolicyDecision(BaseModel):
    decision: str  # allow | deny | require_handoff
    reason_code: str


@dataclass(slots=True)
class AgentPolicyService:
    session: AsyncSession

    async def evaluate_reply(self, snapshot: DialogStateSnapshot) -> PolicyDecision:
        text_blob = " ".join((m.get("body") or "") for m in snapshot.messages).lower()
        if any(flag in text_blob for flag in ["spam", "scam", "fraud"]):
            decision = PolicyDecision(decision="deny", reason_code="risk_flag_detected")
        elif snapshot.active_handoff_status in {"open", "pending"}:
            decision = PolicyDecision(
                decision="require_handoff", reason_code="handoff_active"
            )
        elif snapshot.dialog_status in {"closed", "ignored"}:
            decision = PolicyDecision(decision="deny", reason_code="dialog_not_actionable")
        else:
            decision = PolicyDecision(decision="allow", reason_code="safe_to_reply")

        await self._audit(snapshot.dialog_id, decision)
        await self.session.commit()
        return decision

    async def _audit(self, dialog_id: str, decision: PolicyDecision) -> None:
        log = AgentActionLog(
            dialog_id=dialog_id,
            action_type="policy.evaluate_reply",
            payload_json=json.dumps(decision.model_dump()),
            status=decision.decision,
            reasoning=decision.reason_code,
        )
        self.session.add(log)
        await self.session.flush()
