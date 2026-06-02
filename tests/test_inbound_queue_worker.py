import asyncio
from datetime import UTC, datetime, timedelta

from app.services.inbound_queue_worker import InboundQueueWorker, is_restart_command


class Event:
    def __init__(self, payload=None, *, status="queued", attempt_count=0, max_attempts=5):
        self.payload = payload or {}
        self.status = status
        self.attempt_count = attempt_count
        self.max_attempts = max_attempts
        self.error_message = None
        self.last_error_type = None
        self.processed_at = None
        self.next_attempt_at = None
        self.lease_owner = None
        self.lease_expires_at = None


class SharedClaimRepository:
    def __init__(self, events):
        self.events = events

    async def claim_ready_batch(self, *, lease_owner: str, limit: int = 100, lease_seconds: int = 60):
        now = datetime.now(UTC)
        claimed = []
        for event in self.events:
            if len(claimed) >= limit:
                break
            ready = event.status == "queued" or (
                event.status == "retry" and event.next_attempt_at and event.next_attempt_at <= now
            )
            lease_free = event.lease_expires_at is None or event.lease_expires_at <= now
            if ready and lease_free:
                event.lease_owner = lease_owner
                event.lease_expires_at = now + timedelta(seconds=lease_seconds)
                claimed.append(event)
        return claimed


class FakeSession:
    def __init__(self):
        self.flush_calls = 0
        self.committed = False

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.committed = True


def test_two_workers_do_not_process_same_events() -> None:
    events = [Event(payload={}), Event(payload={})]
    repo = SharedClaimRepository(events)

    s1, s2 = FakeSession(), FakeSession()
    w1 = InboundQueueWorker(s1, lease_owner="w1", lease_seconds=60)
    w2 = InboundQueueWorker(s2, lease_owner="w2", lease_seconds=60)
    w1.repository = repo
    w2.repository = repo

    r1 = asyncio.run(w1.process_queued_batch(limit=2))
    r2 = asyncio.run(w2.process_queued_batch(limit=2))

    assert r1.total == 2
    assert r2.total == 0


def test_lease_expiration_allows_reclaim() -> None:
    event = Event(payload={"simulate": "retry"}, status="retry")
    event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    event.lease_owner = "stale"
    event.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    repo = SharedClaimRepository([event])
    session = FakeSession()
    worker = InboundQueueWorker(session, lease_owner="fresh", lease_seconds=60)
    worker.repository = repo

    result = asyncio.run(worker.process_queued_batch(limit=1))

    assert result.total == 1
    assert event.status in {"retry", "dead_letter"}


def test_reclaim_after_expiry() -> None:
    event = Event(payload={})
    repo = SharedClaimRepository([event])

    s1 = FakeSession()
    w1 = InboundQueueWorker(s1, lease_owner="w1", lease_seconds=1)
    w1.repository = repo
    asyncio.run(w1.process_queued_batch(limit=1))

    event.status = "retry"
    event.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    event.lease_owner = "w1"
    event.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    s2 = FakeSession()
    w2 = InboundQueueWorker(s2, lease_owner="w2", lease_seconds=60)
    w2.repository = repo
    result = asyncio.run(w2.process_queued_batch(limit=1))

    assert result.total == 1


def test_backoff_is_exponential_with_jitter_bounds() -> None:
    worker = InboundQueueWorker(FakeSession())

    b1 = worker._compute_backoff_seconds(1)
    b2 = worker._compute_backoff_seconds(2)
    b3 = worker._compute_backoff_seconds(3)

    assert 4 <= b1 <= 6
    assert 8 <= b2 <= 12
    assert 16 <= b3 <= 24


def test_restart_command_detection_is_exact() -> None:
    assert is_restart_command("restart")
    assert is_restart_command(" /restart ")
    assert is_restart_command("ReStaRt")
    assert not is_restart_command("restart please")
    assert not is_restart_command("")
