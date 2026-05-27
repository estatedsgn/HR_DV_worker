from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.brain_v2 import BrainRun
from app.repositories.lead import LeadRepository
from app.services.brain_v2.executor import BrainExecutor
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


class LeadFactResponse(BaseModel):
    key: str
    value: str | None
    value_json: dict[str, Any] | None
    source: str
    confidence: float | None


class LeadCardResponse(BaseModel):
    id: str
    dialog_id: str
    qualification_status: str
    funnel_state: str
    interest_status: str | None
    score: int | None
    summary: str | None
    next_step: str | None
    assigned_to: str | None
    lost_reason: str | None
    do_not_contact_reason: str | None
    facts: list[LeadFactResponse]


class LeadStatusUpdateRequest(BaseModel):
    funnel_state: str | None = None
    qualification_status: str | None = None
    interest_status: str | None = None
    assigned_to: str | None = None
    next_step: str | None = None


class BrainRunResponse(BaseModel):
    id: str
    status: str
    shadow_mode: bool
    lead_id: str
    dialog_id: str
    incoming_message_id: str | None
    stage_before: str | None
    stage_after: str | None
    dialogue_move: str | None
    validator_verdict: str | None
    response_text: str | None
    router_result: dict[str, Any] | None
    retrieved_card_ids: list[str]
    brain_decision: dict[str, Any] | None
    validator_result: dict[str, Any] | None
    executor_action: dict[str, Any] | None
    state_patch: dict[str, Any] | None
    error_message: str | None


class BrainRunRejectRequest(BaseModel):
    reason: str | None = None


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


@router.get("/leads/{dialog_id}", response_model=LeadCardResponse)
async def get_lead_card(
    dialog_id: str,
    session: AsyncSession = Depends(get_session),
) -> LeadCardResponse:
    lead = await LeadRepository(session).get_by_dialog_with_facts(dialog_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead_card_response(lead)


@router.patch("/leads/{dialog_id}/status", response_model=LeadCardResponse)
async def update_lead_status(
    dialog_id: str,
    request: LeadStatusUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> LeadCardResponse:
    lead = await LeadRepository(session).get_by_dialog_with_facts(dialog_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    for field, value in request.model_dump(exclude_none=True).items():
        setattr(lead, field, value)
    await session.commit()
    lead = await LeadRepository(session).get_by_dialog_with_facts(dialog_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead_card_response(lead)


def lead_card_response(lead) -> LeadCardResponse:
    return LeadCardResponse(
        id=str(lead.id),
        dialog_id=str(lead.dialog_id),
        qualification_status=lead.qualification_status,
        funnel_state=lead.funnel_state,
        interest_status=lead.interest_status,
        score=lead.score,
        summary=lead.summary,
        next_step=lead.next_step,
        assigned_to=lead.assigned_to,
        lost_reason=lead.lost_reason,
        do_not_contact_reason=lead.do_not_contact_reason,
        facts=[
            LeadFactResponse(
                key=fact.fact_key,
                value=fact.fact_value,
                value_json=fact.fact_value_json,
                source=fact.source,
                confidence=fact.confidence,
            )
            for fact in lead.facts
        ],
    )


@router.get("/brain-runs/{brain_run_id}", response_model=BrainRunResponse)
async def get_brain_run(
    brain_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> BrainRunResponse:
    run = await session.get(BrainRun, brain_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brain run not found")
    return brain_run_response(run)


@router.post("/brain-runs/{brain_run_id}/approve", response_model=BrainRunResponse)
async def approve_brain_run(
    brain_run_id: str,
    session: AsyncSession = Depends(get_session),
) -> BrainRunResponse:
    run = await session.get(BrainRun, brain_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brain run not found")
    run = await BrainExecutor(session).approve(run)
    await session.commit()
    return brain_run_response(run)


@router.post("/brain-runs/{brain_run_id}/reject", response_model=BrainRunResponse)
async def reject_brain_run(
    brain_run_id: str,
    request: BrainRunRejectRequest,
    session: AsyncSession = Depends(get_session),
) -> BrainRunResponse:
    run = await session.get(BrainRun, brain_run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brain run not found")
    run = await BrainExecutor(session).reject(run, reason=request.reason)
    await session.commit()
    return brain_run_response(run)


def brain_run_response(run: BrainRun) -> BrainRunResponse:
    return BrainRunResponse(
        id=str(run.id),
        status=run.status,
        shadow_mode=run.shadow_mode,
        lead_id=str(run.lead_id),
        dialog_id=str(run.dialog_id),
        incoming_message_id=str(run.incoming_message_id) if run.incoming_message_id else None,
        stage_before=run.stage_before,
        stage_after=run.stage_after,
        dialogue_move=run.dialogue_move,
        validator_verdict=run.validator_verdict,
        response_text=run.response_text,
        router_result=run.router_result,
        retrieved_card_ids=list(run.retrieved_card_ids or []),
        brain_decision=run.brain_decision,
        validator_result=run.validator_result,
        executor_action=run.executor_action,
        state_patch=run.state_patch,
        error_message=run.error_message,
    )
