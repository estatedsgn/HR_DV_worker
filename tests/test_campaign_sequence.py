import asyncio
import uuid
from datetime import UTC, datetime

from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.outbound_job import OutboundJob
import app.services.campaign_sequence as campaign_sequence
from app.services.campaign_sequence import CampaignSequenceService, build_peer_from_dialog
from app.services.lead_intake import normalize_username


class FakeStep:
    def __init__(self):
        self.id = uuid.uuid4()
        self.position = 1
        self.step_type = "fixed_message"
        self.message_text = "hello"
        self.delay_seconds = 0
        self.wait_for_reply = True


class FakeCampaignStepRepository:
    def __init__(self, session):
        self.session = session

    async def list_by_campaign(self, campaign_id):
        return self.session.steps


class FakeOutboundJobRepository:
    def __init__(self, session):
        self.session = session

    async def get_by_sequence_step(self, *, sequence_run_id, campaign_step_id):
        return self.session.existing_job


class FakeSession:
    def __init__(self, dialog, steps, existing_job=None):
        self.dialog = dialog
        self.steps = steps
        self.existing_job = existing_job
        self.added = []
        self.flushed = 0

    async def get(self, model, object_id):
        if model.__name__ == "Dialog":
            return self.dialog
        return None

    def add(self, instance):
        self.added.append(instance)

    async def flush(self):
        self.flushed += 1


def test_build_peer_from_dialog_user_peer() -> None:
    dialog = Dialog(
        account_id="00000000-0000-0000-0000-000000000001",
        crmchat_dialog_id="dialog-1",
        telegram_peer_type="user",
        telegram_peer_id="123",
        telegram_access_hash="hash",
    )

    assert build_peer_from_dialog(dialog) == {
        "_": "inputPeerUser",
        "userId": 123,
        "accessHash": "hash",
    }


def test_build_peer_from_dialog_requires_hash_for_user() -> None:
    dialog = Dialog(
        account_id="00000000-0000-0000-0000-000000000001",
        crmchat_dialog_id="dialog-1",
        telegram_peer_type="user",
        telegram_peer_id="123",
    )

    assert build_peer_from_dialog(dialog) is None


def test_lead_intake_normalizes_username() -> None:
    assert normalize_username("iamnekiy") == "@iamnekiy"
    assert normalize_username(" @iamnekiy ") == "@iamnekiy"


def test_fixed_step_is_idempotent_when_job_already_exists(monkeypatch) -> None:
    step = FakeStep()
    dialog = Dialog(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        crmchat_dialog_id="dialog-1",
        telegram_username="@iamnekiy",
    )
    run = DialogSequenceRun(
        id=uuid.uuid4(),
        dialog_id=dialog.id,
        campaign_id=uuid.uuid4(),
        status="active",
        current_step_position=0,
    )
    existing_job = OutboundJob(
        id=uuid.uuid4(),
        account_id=dialog.account_id,
        dialog_id=dialog.id,
        text="hello",
        scheduled_at=datetime.now(UTC),
    )
    session = FakeSession(dialog, [step], existing_job=existing_job)
    monkeypatch.setattr(campaign_sequence, "CampaignStepRepository", FakeCampaignStepRepository)
    monkeypatch.setattr(campaign_sequence, "OutboundJobRepository", FakeOutboundJobRepository)

    asyncio.run(CampaignSequenceService(session).process_next_steps(run))

    assert run.current_step_position == 1
    assert run.status == "waiting_outbound"
    assert not any(isinstance(item, OutboundJob) for item in session.added)
