import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True, frozen=True)
class CRMChatWebhookEnvelope:
    event_id: str
    event_type: str
    event_date: datetime | None
    workspace_id: str | None
    data: dict[str, Any]
    previous_data: dict[str, Any] | None = None


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify a CRMchat webhook HMAC-SHA256 signature using constant-time comparison."""

    if not signature or not secret:
        return False
    normalized_signature = signature.removeprefix("sha256=")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(normalized_signature, expected)


def parse_webhook_envelope(payload: dict[str, Any]) -> CRMChatWebhookEnvelope:
    return CRMChatWebhookEnvelope(
        event_id=str(payload.get("eventId") or payload.get("id") or ""),
        event_type=str(payload.get("eventType") or payload.get("type") or ""),
        event_date=parse_datetime(payload.get("eventDate") or payload.get("createdAt")),
        workspace_id=optional_str(
            payload.get("workspaceId") or payload.get("workspace_id")
        ),
        data=dict(payload.get("data") or {}),
        previous_data=(
            dict(payload["previousData"])
            if isinstance(payload.get("previousData"), dict)
            else None
        ),
    )


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
