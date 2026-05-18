import hashlib
import hmac

from app.services.crmchat_webhooks import (
    parse_webhook_envelope,
    verify_webhook_signature,
)


def test_verify_webhook_signature() -> None:
    raw_body = b'{"eventId":"evt_1"}'
    secret = "secret"
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(raw_body, signature, secret)
    assert verify_webhook_signature(raw_body, f"sha256={signature}", secret)
    assert not verify_webhook_signature(raw_body, "invalid", secret)


def test_parse_webhook_envelope() -> None:
    envelope = parse_webhook_envelope(
        {
            "eventId": "evt_1",
            "eventType": "contact.updated",
            "eventDate": "2026-05-18T00:00:00Z",
            "workspaceId": "workspace-1",
            "data": {"id": "contact-1"},
            "previousData": {"name": "Old"},
        }
    )

    assert envelope.event_id == "evt_1"
    assert envelope.event_type == "contact.updated"
    assert envelope.workspace_id == "workspace-1"
    assert envelope.data == {"id": "contact-1"}
    assert envelope.previous_data == {"name": "Old"}
