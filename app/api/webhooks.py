import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models.crmchat_webhook_event import CRMChatWebhookEvent
from app.repositories.crmchat_webhook_event import CRMChatWebhookEventRepository
from app.services.crmchat_webhooks import (
    parse_webhook_envelope,
    verify_webhook_signature,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SUPPORTED_CRMCHAT_EVENTS = frozenset(
    {
        "contact.created",
        "contact.updated",
        "contact.deleted",
    }
)

STORED_HEADER_NAMES = {
    "content-type",
    "user-agent",
    "x-webhook-event",
    "x-webhook-id",
}


@router.post("/crmchat", status_code=status.HTTP_202_ACCEPTED)
async def receive_crmchat_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    raw_body = await request.body()
    signature = request.headers.get("X-Webhook-Signature", "")

    if not settings.crmchat_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CRMCHAT_WEBHOOK_SECRET is not configured",
        )
    if not verify_webhook_signature(
        raw_body, signature, settings.crmchat_webhook_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid CRMchat webhook signature",
        )

    payload = parse_json_body(raw_body)
    envelope = parse_webhook_envelope(payload)
    event_id = envelope.event_id or request.headers.get("X-Webhook-Id", "")
    event_type = envelope.event_type or request.headers.get("X-Webhook-Event", "")

    if not event_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CRMchat webhook event id is required",
        )
    if not event_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CRMchat webhook event type is required",
        )

    repository = CRMChatWebhookEventRepository(session)
    existing = await repository.get_by_event_id(event_id)
    if existing:
        return {
            "status": "duplicate",
            "event_id": existing.event_id,
            "event_type": existing.event_type,
        }

    event_status = "received" if event_type in SUPPORTED_CRMCHAT_EVENTS else "ignored"
    event = CRMChatWebhookEvent(
        event_id=event_id,
        event_type=event_type,
        workspace_id=envelope.workspace_id,
        payload=payload,
        headers=capture_headers(request),
        status=event_status,
        processed_at=(datetime.now(UTC) if event_status == "ignored" else None),
    )
    await repository.add(event)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return {
            "status": "duplicate",
            "event_id": event_id,
            "event_type": event_type,
        }

    return {
        "status": "accepted" if event_status == "received" else "ignored",
        "event_id": event_id,
        "event_type": event_type,
    }


def parse_json_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CRMchat webhook body must be valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CRMchat webhook body must be a JSON object",
        )
    return payload


def capture_headers(request: Request) -> dict[str, str]:
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() in STORED_HEADER_NAMES
    }
