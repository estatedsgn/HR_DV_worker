from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.lead_intake import LeadIntakeService

router = APIRouter(prefix="/internal", tags=["internal"])


class LeadIntakeRequest(BaseModel):
    source: str = Field(default="internal", min_length=1, max_length=50)
    external_lead_id: str = Field(min_length=1, max_length=255)
    telegram_username: str = Field(min_length=2, max_length=255)
    campaign_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class LeadIntakeResponse(BaseModel):
    status: str
    idempotent: bool
    event_id: str
    dialog_id: str | None
    account_id: str | None


@router.post("/leads", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_lead(
    request: LeadIntakeRequest,
    session: AsyncSession = Depends(get_session),
) -> LeadIntakeResponse:
    try:
        result = await LeadIntakeService(session).enqueue_lead(
            source=request.source,
            external_lead_id=request.external_lead_id,
            telegram_username=request.telegram_username,
            payload=request.payload,
            campaign_id=request.campaign_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return LeadIntakeResponse(
        status=result.event.status,
        idempotent=result.idempotent,
        event_id=str(result.event.id),
        dialog_id=str(result.event.dialog_id) if result.event.dialog_id else None,
        account_id=str(result.event.account_id) if result.event.account_id else None,
    )
