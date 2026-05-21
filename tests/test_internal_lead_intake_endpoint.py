from collections.abc import AsyncGenerator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import app.api.internal as internal
from app.db.session import get_session
from app.main import app


class FakeSession:
    pass


class FakeLeadIntakeService:
    def __init__(self, session):
        self.session = session

    async def enqueue_lead(self, **kwargs):
        event = SimpleNamespace(
            id=uuid4(),
            status="accepted",
            dialog_id=uuid4(),
            account_id=uuid4(),
        )
        return SimpleNamespace(event=event, idempotent=False)


@pytest.fixture(autouse=True)
def override_dependencies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(internal, "LeadIntakeService", FakeLeadIntakeService)

    async def override_get_session() -> AsyncGenerator[FakeSession, None]:
        yield FakeSession()

    app.dependency_overrides[get_session] = override_get_session
    yield
    app.dependency_overrides.clear()


def test_internal_lead_endpoint_enqueues_lead() -> None:
    response = TestClient(app).post(
        "/internal/leads",
        json={
            "source": "test",
            "external_lead_id": "lead-1",
            "telegram_username": "@iamnekiy",
            "payload": {"kind": "unit"},
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["idempotent"] is False
