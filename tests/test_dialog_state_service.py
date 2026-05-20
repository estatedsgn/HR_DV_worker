import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.services.dialog_state import DialogStateService


class FakeDialog:
    def __init__(self, dialog_id: str, status: str = "open"):
        self.id = dialog_id
        self.status = status


class FakeMessage:
    def __init__(self, message_id: str, body: str):
        self.id = message_id
        self.direction = "inbound"
        self.sender_type = "lead"
        self.body = body
        self.status = "synced"
        self.sent_at = datetime.now(UTC)


class FakeLead:
    def __init__(self, qualification_status: str):
        self.qualification_status = qualification_status


class FakeHandoff:
    def __init__(self, status: str):
        self.status = status


class FakeAction:
    def __init__(self, action_type: str):
        self.action_type = action_type


class FakeAgentActions:
    def __init__(self):
        self.added = []

    async def add(self, instance):
        self.added.append(instance)
        return instance


class FakeLogRepository:
    def __init__(self):
        self.agent_actions = FakeAgentActions()


class FakeMessageRepository:
    def __init__(self, messages):
        self.messages = messages

    async def list_by_dialog(self, dialog_id, limit: int = 20):
        return self.messages[:limit]


class FakeLeadRepository:
    def __init__(self, lead):
        self.lead = lead

    async def get_by_dialog(self, dialog_id):
        return self.lead


class FakeSession:
    def __init__(self, dialog=None):
        self.dialog = dialog
        self.flushed = False
        self.committed = False

    async def get(self, model, obj_id):
        return self.dialog

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


class DialogStateServiceHarness(DialogStateService):
    def __init__(self, session, *, messages, lead, handoff, action):
        super().__init__(session)
        self.message_repository = FakeMessageRepository(messages)
        self.lead_repository = FakeLeadRepository(lead)
        self.log_repository = FakeLogRepository()
        self._handoff = handoff
        self._action = action

    async def _get_active_handoff(self, dialog_id: str):
        return self._handoff

    async def _get_last_agent_action(self, dialog_id: str):
        return self._action


def test_full_snapshot() -> None:
    dialog = FakeDialog(str(uuid.uuid4()), status="open")
    service = DialogStateServiceHarness(
        FakeSession(dialog),
        messages=[FakeMessage(str(uuid.uuid4()), "hello")],
        lead=FakeLead("qualified"),
        handoff=FakeHandoff("open"),
        action=FakeAction("reply"),
    )

    snapshot = asyncio.run(service.get_snapshot(dialog.id))

    assert snapshot.dialog_status == "open"
    assert snapshot.lead_status == "qualified"
    assert snapshot.active_handoff_status == "open"
    assert snapshot.last_agent_action == "reply"
    assert len(snapshot.messages) == 1
    assert snapshot.messages[0]["body"] == "hello"


def test_empty_snapshot_unknown_dialog() -> None:
    service = DialogStateServiceHarness(
        FakeSession(dialog=None), messages=[], lead=None, handoff=None, action=None
    )
    snapshot = asyncio.run(service.get_snapshot(str(uuid.uuid4())))
    assert snapshot.dialog_status == "unknown"
    assert snapshot.lead_status == "unknown"
    assert snapshot.messages == []


def test_transition_with_validation_and_audit_log() -> None:
    dialog = FakeDialog(str(uuid.uuid4()), status="open")
    session = FakeSession(dialog)
    service = DialogStateServiceHarness(
        session,
        messages=[],
        lead=None,
        handoff=None,
        action=FakeAction("state_transition"),
    )

    snapshot = asyncio.run(
        service.update_dialog_status(
            dialog.id, new_status="in_progress", reason="picked", actor="worker"
        )
    )
    assert snapshot.dialog_status == "in_progress"
    assert session.flushed is True
    assert session.committed is True
    assert len(service.log_repository.agent_actions.added) == 1

    with pytest.raises(ValueError, match="Unsupported dialog status"):
        asyncio.run(service.update_dialog_status(dialog.id, new_status="bad_status"))
