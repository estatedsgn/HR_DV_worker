import hashlib
import hmac
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient

import app.api.webhooks as webhooks
from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.main import app


class FakeSession:
    committed = False
    rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeWebhookEventRepository:
    events = {}

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def get_by_event_id(self, event_id: str):
        return self.events.get(event_id)

    async def add(self, instance):
        self.events[instance.event_id] = instance
        return instance


@pytest.fixture(autouse=True)
def override_dependencies(monkeypatch: pytest.MonkeyPatch):
    FakeWebhookEventRepository.events = {}
    monkeypatch.setattr(
        webhooks, "CRMChatWebhookEventRepository", FakeWebhookEventRepository
    )

    async def override_get_session() -> AsyncGenerator[FakeSession, None]:
        yield FakeSession()

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        CRMCHAT_WEBHOOK_SECRET="secret"
    )
    yield
    app.dependency_overrides.clear()


def signature_for(raw_body: bytes, secret: str = "secret") -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def test_crmchat_webhook_accepts_signed_contact_event() -> None:
    raw_body = (
        b'{"eventId":"evt_1","eventType":"contact.created",'
        b'"eventDate":"2026-05-18T00:00:00Z",'
        b'"workspaceId":"workspace-1","data":{"id":"contact-1"}}'
    )

    response = TestClient(app).post(
        "/webhooks/crmchat",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature_for(raw_body),
            "X-Webhook-Event": "contact.created",
            "X-Webhook-Id": "evt_1",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "event_id": "evt_1",
        "event_type": "contact.created",
    }
    stored = FakeWebhookEventRepository.events["evt_1"]
    assert stored.event_type == "contact.created"
    assert stored.workspace_id == "workspace-1"
    assert stored.status == "received"
    assert stored.payload["data"] == {"id": "contact-1"}
    assert "x-webhook-signature" not in stored.headers


def test_crmchat_webhook_rejects_invalid_signature() -> None:
    raw_body = b'{"eventId":"evt_1","eventType":"contact.updated"}'

    response = TestClient(app).post(
        "/webhooks/crmchat",
        content=raw_body,
        headers={"X-Webhook-Signature": "invalid"},
    )

    assert response.status_code == 401
    assert FakeWebhookEventRepository.events == {}


def test_crmchat_webhook_is_idempotent_by_event_id() -> None:
    raw_body = b'{"eventId":"evt_1","eventType":"contact.deleted"}'
    headers = {"X-Webhook-Signature": signature_for(raw_body)}
    client = TestClient(app)

    first = client.post("/webhooks/crmchat", content=raw_body, headers=headers)
    second = client.post("/webhooks/crmchat", content=raw_body, headers=headers)

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert second.status_code == 202
    assert second.json() == {
        "status": "duplicate",
        "event_id": "evt_1",
        "event_type": "contact.deleted",
    }
    assert len(FakeWebhookEventRepository.events) == 1


def test_crmchat_webhook_stores_unknown_events_as_ignored() -> None:
    raw_body = b'{"eventId":"evt_1","eventType":"message.created"}'

    response = TestClient(app).post(
        "/webhooks/crmchat",
        content=raw_body,
        headers={"X-Webhook-Signature": signature_for(raw_body)},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "ignored"
    assert FakeWebhookEventRepository.events["evt_1"].status == "ignored"
