import asyncio

from app.repositories.inbound_event import InboundEventRepository


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def fetchall(self):
        return self.value


class FakeSession:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def execute(self, statement, params=None):
        self.calls += 1
        return ScalarResult(self.value)


class Existing:
    pass


def test_get_duplicate_returns_none_when_both_keys_missing() -> None:
    session = FakeSession(Existing())
    repo = InboundEventRepository(session)
    result = asyncio.run(repo.get_duplicate(external_event_id=None, external_message_id=None))
    assert result is None
    assert session.calls == 0


def test_get_duplicate_by_external_event_id() -> None:
    existing = Existing()
    session = FakeSession(existing)
    repo = InboundEventRepository(session)
    result = asyncio.run(repo.get_duplicate(external_event_id="evt-1", external_message_id=None))
    assert result is existing


def test_get_duplicate_by_external_message_id() -> None:
    existing = Existing()
    session = FakeSession(existing)
    repo = InboundEventRepository(session)
    result = asyncio.run(repo.get_duplicate(external_event_id=None, external_message_id="msg-1"))
    assert result is existing


def test_claim_ready_batch_returns_empty_when_nothing_claimed() -> None:
    session = FakeSession([])
    repo = InboundEventRepository(session)
    result = asyncio.run(repo.claim_ready_batch(lease_owner="w1", limit=10, lease_seconds=30))
    assert result == []
