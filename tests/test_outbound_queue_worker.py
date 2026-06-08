import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.models.account import Account
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.outbound_queue_worker import (
    OutboundQueueWorker,
    OutboundSendBlockedError,
    normalize_username,
)


class FakeOutboundRepository:
    def __init__(self, jobs):
        self.jobs = jobs

    async def claim_ready_batch(
        self, *, lease_owner: str, limit: int = 50, lease_seconds: int = 60, account_id=None
    ):
        for job in self.jobs[:limit]:
            job.lease_owner = lease_owner
            job.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        return self.jobs[:limit]


class FakeSession:
    def __init__(self, account, message=None):
        self.account = account
        self.message = message
        self.added = []
        self.committed = False

    async def get(self, model, object_id):
        if model.__name__ == "Account" and object_id == self.account.id:
            return self.account
        if self.message is not None and model.__name__ == "Message" and object_id == self.message.id:
            return self.message
        return None

    async def execute(self, query):
        return FakeScalarResult(None)

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeConnector:
    def __init__(self):
        self.typing_actions = []
        self.voice_notes = []

    async def set_typing(self, workspace_id, account_id, peer, action="sendMessageTypingAction"):
        self.typing_actions.append(action)
        return {"ok": True}

    async def send_message(self, workspace_id, account_id, peer, message, random_id):
        return {"_": "updates", "random_id": random_id}

    async def send_voice_note(
        self,
        workspace_id,
        account_id,
        peer,
        path,
        random_id,
        *,
        caption="",
        duration_seconds=0,
    ):
        self.voice_notes.append(
            {
                "path": str(path),
                "random_id": random_id,
                "caption": caption,
                "duration_seconds": duration_seconds,
            }
        )
        return {"_": "updates", "random_id": random_id}

    async def aclose(self):
        return None


def make_account() -> Account:
    return Account(
        id=uuid.uuid4(),
        crmchat_account_id="crm-account-1",
        crmchat_workspace_id="workspace-1",
        status="active",
        health_status="healthy",
        send_interval_seconds=10,
        send_jitter_seconds=0,
    )


def test_normalize_username() -> None:
    assert normalize_username("IamNekiy") == "@iamnekiy"
    assert normalize_username("@IamNekiy") == "@iamnekiy"


def test_text_typing_delay_is_random_between_min_and_max(monkeypatch) -> None:
    calls = []

    def fake_uniform(low, high):
        calls.append((low, high))
        return 7.5

    monkeypatch.setattr("app.services.outbound_queue_worker.random.uniform", fake_uniform)
    worker = OutboundQueueWorker(
        FakeSession(make_account()),
        connector=FakeConnector(),
        settings=Settings(
            OUTBOUND_ALLOWED_USERNAMES="@iamnekiy",
            OUTBOUND_TYPING_MIN_DELAY_SECONDS=5,
        ),
        allow_real_send=True,
        typing_delay_seconds=10,
    )

    assert worker._text_typing_delay_seconds() == 7.5
    assert calls == [(5.0, 10.0)]


def test_send_guard_blocks_non_daivinchik_non_allowlisted() -> None:
    # Диалог не из разрешённого источника (session.get(Dialog) -> None) и username
    # не в allowlist -> блок. Это обычный telegram:-собеседник (бот/случайный).
    worker = OutboundQueueWorker(
        FakeSession(make_account()),
        connector=FakeConnector(),
        settings=Settings(OUTBOUND_ALLOWED_USERNAMES="@iamnekiy"),
        allow_real_send=True,
    )
    job = OutboundJob(target_username="@other", text="hello", scheduled_at=datetime.now(UTC))

    with pytest.raises(OutboundSendBlockedError):
        asyncio.run(worker._assert_send_allowed(job))


def test_send_guard_allows_daivinchik_intake_dialog() -> None:
    # Диалог заведён интейком daivinchik -> аутрич разрешён даже без username в
    # allowlist (динамический лид по взаимной симпатии).
    from app.models.dialog import Dialog

    account = make_account()
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=account.id,
        crmchat_dialog_id="intake:daivinchik:@yulia_k",
        telegram_username="@yulia_k",
        status="open",
    )
    session = FakeSession(account)

    async def get(model, object_id):
        if model.__name__ == "Dialog" and object_id == dialog.id:
            return dialog
        if model.__name__ == "Account" and object_id == account.id:
            return account
        return None

    session.get = get  # type: ignore[assignment]
    worker = OutboundQueueWorker(
        session,
        connector=FakeConnector(),
        settings=Settings(OUTBOUND_ALLOWED_USERNAMES=""),
        allow_real_send=True,
    )
    job = OutboundJob(
        dialog_id=dialog.id,
        target_username="@yulia_k",
        text="hello",
        scheduled_at=datetime.now(UTC),
    )
    # не бросает -> отправка разрешена
    asyncio.run(worker._assert_send_allowed(job))


def test_send_guard_blocks_when_real_send_disabled() -> None:
    worker = OutboundQueueWorker(
        FakeSession(make_account()),
        connector=FakeConnector(),
        settings=Settings(OUTBOUND_ALLOWED_USERNAMES="@iamnekiy"),
        allow_real_send=False,
    )
    job = OutboundJob(target_username="@iamnekiy", text="hello", scheduled_at=datetime.now(UTC))

    with pytest.raises(OutboundSendBlockedError):
        asyncio.run(worker._assert_send_allowed(job))


def test_worker_sends_allowlisted_job_and_updates_message() -> None:
    account = make_account()
    message = Message(
        id=uuid.uuid4(),
        dialog_id=uuid.uuid4(),
        direction="outbound",
        sender_type="agent",
        body="hello",
        status="scheduled",
    )
    job = OutboundJob(
        id=uuid.uuid4(),
        account_id=account.id,
        dialog_id=message.dialog_id,
        message_id=message.id,
        target_username="@iamnekiy",
        peer={"_": "inputPeerUser", "userId": 1, "accessHash": "hash"},
        text="hello",
        status="queued",
        scheduled_at=datetime.now(UTC),
        max_attempts=3,
    )
    session = FakeSession(account, message)
    connector = FakeConnector()
    worker = OutboundQueueWorker(
        session,
        connector=connector,
        settings=Settings(
            OUTBOUND_ALLOWED_USERNAMES="@iamnekiy",
            OUTBOUND_TYPING_MIN_DELAY_SECONDS=0,
        ),
        allow_real_send=True,
        typing_delay_seconds=0.001,
    )
    worker.repository = FakeOutboundRepository([job])

    result = asyncio.run(worker.process_queued_batch(limit=1))

    assert result.sent == 1
    assert job.status == "sent"
    assert message.status == "sent"
    assert connector.typing_actions == ["sendMessageTypingAction"]
    assert account.next_available_at is not None
    assert session.committed is True


def test_worker_sends_voice_job_with_recording_action() -> None:
    account = make_account()
    message = Message(
        id=uuid.uuid4(),
        dialog_id=uuid.uuid4(),
        direction="outbound",
        sender_type="agent",
        body="[voice] intro",
        status="scheduled",
    )
    job = OutboundJob(
        id=uuid.uuid4(),
        account_id=account.id,
        dialog_id=message.dialog_id,
        message_id=message.id,
        target_username="@iamnekiy",
        peer={"_": "inputPeerUser", "userId": 1, "accessHash": "hash"},
        job_type="voice",
        text="[voice] intro",
        media_path="data/voice_intro/voice_intro_01_offer_overview.ogg",
        media_mime_type="audio/ogg",
        media_metadata={"caption": "", "recording_delay_seconds": 0.001, "duration_seconds": 11},
        typing_action="sendMessageRecordAudioAction",
        status="queued",
        scheduled_at=datetime.now(UTC),
        max_attempts=3,
    )
    session = FakeSession(account, message)
    connector = FakeConnector()
    worker = OutboundQueueWorker(
        session,
        connector=connector,
        settings=Settings(OUTBOUND_ALLOWED_USERNAMES="@iamnekiy"),
        allow_real_send=True,
    )
    worker.repository = FakeOutboundRepository([job])

    result = asyncio.run(worker.process_queued_batch(limit=1))

    assert result.sent == 1
    assert connector.typing_actions == ["sendMessageRecordAudioAction"]
    assert connector.voice_notes[0]["path"].endswith("voice_intro_01_offer_overview.ogg")
    assert connector.voice_notes[0]["duration_seconds"] == 11
    assert job.status == "sent"
    assert message.status == "sent"
