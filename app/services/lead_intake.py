from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.lead import Lead
from app.models.lead_intake_event import LeadIntakeEvent
from app.repositories.account import AccountRepository
from app.repositories.campaign import CampaignRepository
from app.repositories.dialog import DialogRepository
from app.repositories.lead import LeadRepository
from app.repositories.lead_intake_event import LeadIntakeEventRepository
from app.services.campaign_defaults import DefaultCampaignService
from app.services.campaign_sequence import CampaignSequenceService


@dataclass(slots=True, frozen=True)
class LeadIntakeResult:
    event: LeadIntakeEvent
    idempotent: bool


class LeadIntakeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue_lead(
        self,
        *,
        source: str,
        external_lead_id: str,
        telegram_username: str,
        payload: dict[str, Any] | None = None,
        campaign_id: str | None = None,
    ) -> LeadIntakeResult:
        normalized_username = normalize_username(telegram_username)
        repository = LeadIntakeEventRepository(self.session)
        duplicate = await repository.get_duplicate(
            source=source, external_lead_id=external_lead_id
        )
        if duplicate:
            return LeadIntakeResult(event=duplicate, idempotent=True)

        campaign = (
            await CampaignRepository(self.session).get_with_steps(campaign_id)
            if campaign_id
            else await DefaultCampaignService(self.session).ensure_default_campaign()
        )
        if campaign is None:
            raise ValueError(f"Campaign not found: {campaign_id}")

        account = await AccountRepository(self.session).get_available_for_assignment()
        if account is None:
            event = LeadIntakeEvent(
                source=source,
                external_lead_id=external_lead_id,
                telegram_username=normalized_username,
                campaign_id=campaign.id,
                status="failed",
                payload=payload or {},
                error_message="No active healthy account is available",
            )
            await repository.add(event)
            await self.session.commit()
            return LeadIntakeResult(event=event, idempotent=False)

        event = LeadIntakeEvent(
            source=source,
            external_lead_id=external_lead_id,
            telegram_username=normalized_username,
            campaign_id=campaign.id,
            account_id=account.id,
            status="accepted",
            payload=payload or {},
        )
        dialog = await self._get_or_create_dialog(
            account_id=account.id,
            source=source,
            external_lead_id=external_lead_id,
            telegram_username=normalized_username,
        )
        try:
            event.dialog_id = dialog.id
            await repository.add(event)
            await self._ensure_lead(dialog)
            run = DialogSequenceRun(
                dialog_id=dialog.id,
                campaign_id=campaign.id,
                lead_intake_event_id=event.id,
                status="active",
                current_step_position=0,
                started_at=datetime.now(UTC),
            )
            self.session.add(run)
            await self.session.flush()
            await CampaignSequenceService(self.session).start(run)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            duplicate = await repository.get_duplicate(
                source=source, external_lead_id=external_lead_id
            )
            if duplicate:
                return LeadIntakeResult(event=duplicate, idempotent=True)
            raise
        return LeadIntakeResult(event=event, idempotent=False)

    async def _get_or_create_dialog(
        self,
        *,
        account_id,
        source: str,
        external_lead_id: str,
        telegram_username: str,
    ) -> Dialog:
        crmchat_dialog_id = f"intake:{source}:{external_lead_id}"
        repository = DialogRepository(self.session)
        existing = await repository.get_by_crmchat_dialog_id(crmchat_dialog_id)
        if existing:
            return existing
        dialog = Dialog(
            account_id=account_id,
            crmchat_dialog_id=crmchat_dialog_id,
            lead_external_id=external_lead_id,
            telegram_username=telegram_username,
            status="open",
        )
        return await repository.add(dialog)

    async def _ensure_lead(self, dialog: Dialog) -> None:
        existing = await LeadRepository(self.session).get_by_dialog(dialog.id)
        if existing:
            return
        self.session.add(Lead(dialog_id=dialog.id, qualification_status="new"))
        await self.session.flush()


def normalize_username(value: str) -> str:
    username = value.strip()
    if not username:
        raise ValueError("telegram_username is required")
    return username if username.startswith("@") else f"@{username}"
