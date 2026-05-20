import asyncio
import json
import uuid
from datetime import UTC, datetime

from app.services.agent_toolkit import (
    AgentToolkitService,
    ProposeReplyInput,
    ReadDialogContextInput,
    RequestHumanHandoffInput,
)


class FakeDialog:
    def __init__(self, dialog_id: str, status: str = "open"):
        self.id = dialog_id
        self.status = status


class FakeMessage:
    def __init__(self, body: str):
        self.id = uuid.uuid4()
        self.direction = "inbound"
        self.sender_type = "lead"
        self.body = body
        self.status = "synced"
        self.sent_at = datetime.now(UTC)


class FakeLead:
    def __init__(self, status: str):
        self.qualification_status = status


class FakeHandoff:
    def __init__(self, handoff_id: str, dialog_id: str, reason: str, status: str = "open"):
        self.id = handoff_id
        self.dialog_id = dialog_id
        self.reason = reason
        self.status = status


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, dialog):
        self.dialog = dialog
        self.handoffs = []
        self.logs = []
        self.committed = 0

    async def get(self, model, obj_id):
        if self.dialog and str(self.dialog.id) == str(obj_id):
            return self.dialog
        return None

    async def execute(self, query):
        # Used by _find_open_handoff in toolkit service.
        if self.handoffs:
            return FakeResult(self.handoffs[-1])
        return FakeResult(None)

    def add(self, instance):
        name = instance.__class__.__name__
        if name == "HumanHandoff":
            if not getattr(instance, "id", None):
                instance.id = uuid.uuid4()
            self.handoffs.append(instance)
        elif name == "AgentActionLog":
            self.logs.append(instance)

    async def flush(self):
        return None

    async def commit(self):
        self.committed += 1


class HarnessToolkitService(AgentToolkitService):
    def __init__(self, session, *, messages, lead_status, handoff_status, last_action):
        super().__init__(session)
        self._messages = messages
        self._lead_status = lead_status
        self._handoff_status = handoff_status
        self._last_action = last_action

    async def read_dialog_context(self, payload: ReadDialogContextInput):
        # keep schema + audit behavior from parent but inject deterministic snapshot data
        dialog = await self.session.get(None, payload.dialog_id)
        if dialog is None:
            from app.services.agent_toolkit import ReadDialogContextOutput

            await self._audit(payload.dialog_id, "tool.read_dialog_context", {"message_limit": payload.message_limit}, status="completed")
            await self.session.commit()
            return ReadDialogContextOutput(
                dialog_id=payload.dialog_id,
                dialog_status="unknown",
                lead_status="unknown",
                active_handoff_status=None,
                last_agent_action=None,
                messages=[],
            )

        from app.services.agent_toolkit import ReadDialogContextOutput

        messages = [
            {
                "id": str(m.id),
                "direction": m.direction,
                "sender_type": m.sender_type,
                "body": m.body,
                "status": m.status,
                "sent_at": m.sent_at.isoformat(),
            }
            for m in self._messages
        ]
        await self._audit(payload.dialog_id, "tool.read_dialog_context", {"message_limit": payload.message_limit}, status="completed")
        await self.session.commit()
        return ReadDialogContextOutput(
            dialog_id=str(dialog.id),
            dialog_status=dialog.status,
            lead_status=self._lead_status,
            active_handoff_status=self._handoff_status,
            last_agent_action=self._last_action,
            messages=messages,
        )

    async def propose_reply(self, payload):
        from app.services.agent_toolkit import ProposeReplyOutput

        text = self._build_reply("open", self._lead_status, payload.strategy)
        output = ProposeReplyOutput(
            dialog_id=payload.dialog_id,
            strategy=payload.strategy,
            proposed_reply=text,
            safe=True,
        )
        await self._audit(payload.dialog_id, "tool.propose_reply", output.model_dump(), status="completed")
        await self.session.commit()
        return output


def test_read_propose_handoff_integration_flow() -> None:
    dialog_id = str(uuid.uuid4())
    session = FakeSession(FakeDialog(dialog_id, "open"))
    toolkit = HarnessToolkitService(
        session,
        messages=[FakeMessage("Добрый день")],
        lead_status="qualified",
        handoff_status=None,
        last_action="wait",
    )

    context = asyncio.run(toolkit.read_dialog_context(ReadDialogContextInput(dialog_id=dialog_id)))
    assert context.dialog_status == "open"
    assert context.lead_status == "qualified"

    proposal = asyncio.run(
        toolkit.propose_reply(ProposeReplyInput(dialog_id=dialog_id, strategy="clarify"))
    )
    assert proposal.safe is True
    assert "Уточните" in proposal.proposed_reply

    handoff = asyncio.run(
        toolkit.request_human_handoff(
            RequestHumanHandoffInput(dialog_id=dialog_id, reason="Need manager")
        )
    )
    assert handoff.idempotent is False

    handoff_dup = asyncio.run(
        toolkit.request_human_handoff(
            RequestHumanHandoffInput(dialog_id=dialog_id, reason="Need manager")
        )
    )
    assert handoff_dup.idempotent is True
    assert handoff_dup.handoff_id == handoff.handoff_id

    assert len(session.logs) >= 4
    payloads = [json.loads(log.payload_json or "{}") for log in session.logs]
    assert any("message_limit" in p for p in payloads)


def test_read_unknown_dialog_safe_fallback() -> None:
    toolkit = HarnessToolkitService(
        FakeSession(dialog=None),
        messages=[],
        lead_status="unknown",
        handoff_status=None,
        last_action=None,
    )

    result = asyncio.run(
        toolkit.read_dialog_context(ReadDialogContextInput(dialog_id=str(uuid.uuid4())))
    )
    assert result.dialog_status == "unknown"
    assert result.messages == []
