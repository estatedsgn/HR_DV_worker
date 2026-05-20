import asyncio

from sqlalchemy.exc import IntegrityError

from app.services.inbound_pipeline import InboundPipelineService


class DummySession:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


class ExistingEvent:
    id = "existing-id"


class FakeRepository:
    def __init__(self, duplicate_before=None, duplicate_after=None, raise_integrity=False):
        self.duplicate_before = duplicate_before
        self.duplicate_after = duplicate_after
        self.raise_integrity = raise_integrity
        self.add_calls = 0
        self.get_calls = 0

    async def get_duplicate(self, *, external_event_id, external_message_id):
        self.get_calls += 1
        if self.get_calls == 1:
            return self.duplicate_before
        return self.duplicate_after

    async def add(self, instance):
        self.add_calls += 1
        if self.raise_integrity:
            raise IntegrityError("insert", {}, Exception("duplicate"))
        return instance


def test_enqueue_creates_new_event_when_not_duplicate() -> None:
    service = InboundPipelineService(DummySession())
    service.repository = FakeRepository(duplicate_before=None, raise_integrity=False)

    event, is_new = asyncio.run(
        service.enqueue(
            source="crmchat_webhook",
            payload={"a": 1},
            external_event_id="evt-new",
        )
    )

    assert is_new is True
    assert event.external_event_id == "evt-new"
    assert service.repository.add_calls == 1


def test_enqueue_returns_existing_duplicate_without_insert() -> None:
    service = InboundPipelineService(DummySession())
    existing = ExistingEvent()
    service.repository = FakeRepository(duplicate_before=existing)

    event, is_new = asyncio.run(
        service.enqueue(
            source="crmchat_webhook",
            payload={"a": 1},
            external_event_id="evt-1",
        )
    )

    assert event is existing
    assert is_new is False
    assert service.repository.add_calls == 0


def test_enqueue_handles_integrity_error_and_returns_existing() -> None:
    session = DummySession()
    service = InboundPipelineService(session)
    existing = ExistingEvent()
    service.repository = FakeRepository(
        duplicate_before=None,
        duplicate_after=existing,
        raise_integrity=True,
    )

    event, is_new = asyncio.run(
        service.enqueue(
            source="telegram_polling",
            payload={"a": 1},
            external_message_id="msg-1",
        )
    )

    assert session.rolled_back is True
    assert event is existing
    assert is_new is False
    assert service.repository.add_calls == 1
