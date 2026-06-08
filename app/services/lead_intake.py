from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.lead import Lead
from app.models.lead_intake_event import LeadIntakeEvent
from app.models.account import Account
from app.repositories.account import AccountRepository
from app.repositories.campaign import CampaignRepository, CampaignStepRepository
from app.repositories.dialog import DialogRepository
from app.repositories.lead import LeadRepository
from app.repositories.lead_intake_event import LeadIntakeEventRepository
from app.services.campaign_defaults import DefaultCampaignService
from app.services.campaign_sequence import CampaignSequenceService
from app.services.lead_notifier import LeadNotifier


@dataclass(slots=True, frozen=True)
class LeadIntakeResult:
    event: LeadIntakeEvent
    idempotent: bool


class LeadIntakeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        notifier: LeadNotifier | None = None,
    ) -> None:
        self.session = session
        self.notifier = notifier or LeadNotifier()

    async def enqueue_lead(
        self,
        *,
        source: str,
        external_lead_id: str,
        telegram_username: str,
        payload: dict[str, Any] | None = None,
        campaign_id: str | None = None,
        account_id: str | None = None,
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

        # The lead MUST be served by the account that actually matched it on the
        # source (e.g. the Дайвинчик account that got the mutual symatch) — that's
        # the only account with a real conversation/peer relationship. Picking any
        # "available" account would make a different account message a stranger.
        # When the caller doesn't bind one (legacy single-account path), fall back
        # to the previous "next available" heuristic.
        account = await self._select_account(account_id)
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
            await self.notifier.notify_intake_blocked(
                telegram_username=normalized_username,
                source=source,
                reason="Нет свободного активного аккаунта",
            )
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
            await self.session.flush()

            settings = get_settings()
            if settings.langgraph_funnel_enabled:
                # Все лиды (в т.ч. взаимные симпатии с Дайвинчика) заводятся прямо
                # в живую LangGraph-воронку: она отправляет правильный first-touch
                # опенер и ведёт лида по стадиям. Это тот же мозг, что отвечает на
                # входящие, поэтому весь диалог идёт по одной воронке, а не по
                # легаси-лестнице кампаний (которая слала шаблонное «Здравствуйте…»).
                from app.services.funnel_graph.gateway import LangGraphFunnelGateway

                await LangGraphFunnelGateway(self.session, settings=settings).start_for_dialog(
                    dialog_id=str(dialog.id)
                )
                first_message = await self._funnel_first_touch(normalized_username)
            else:
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
                first_message = await self._first_message_text(campaign.id)
            await self.session.commit()
            await self.notifier.notify_new_lead(
                telegram_username=normalized_username,
                account_label=account_label(account),
                source=source,
                first_message=first_message,
            )
        except IntegrityError:
            await self.session.rollback()
            duplicate = await repository.get_duplicate(
                source=source, external_lead_id=external_lead_id
            )
            if duplicate:
                return LeadIntakeResult(event=duplicate, idempotent=True)
            raise
        return LeadIntakeResult(event=event, idempotent=False)

    async def _select_account(self, account_id: str | None) -> Account | None:
        """Pick the account that will serve this lead.

        With an explicit account_id (the account that captured the match) we bind
        to it directly — even if it's currently pacing/rate-limited, because the
        conversation can only continue from that account. Without one we keep the
        legacy "next available healthy account" behaviour.
        """
        repository = AccountRepository(self.session)
        if account_id:
            account = await repository.get(account_id)
            if account is not None:
                return account
        return await repository.get_available_for_assignment()

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

    async def _first_message_text(self, campaign_id) -> str | None:
        steps = await CampaignStepRepository(self.session).list_by_campaign(campaign_id)
        for step in sorted(steps, key=lambda item: item.position):
            if step.step_type == "fixed_message" and step.message_text:
                return step.message_text
        return None

    async def _funnel_first_touch(self, candidate_id: str | None) -> str | None:
        """The actual opener the funnel will send (rotated per candidate) — for the
        supervisor 'new lead' notification only."""
        try:
            from app.services.funnel_graph.knowledge import StaticFunnelKnowledgeBase

            return StaticFunnelKnowledgeBase().first_touch(candidate_id) or None
        except Exception:  # noqa: BLE001
            return None

    async def _ensure_lead(self, dialog: Dialog) -> None:
        existing = await LeadRepository(self.session).get_by_dialog(dialog.id)
        if existing:
            return
        self.session.add(
            Lead(
                dialog_id=dialog.id,
                qualification_status="new",
                funnel_state="NEW_LEAD",
            )
        )
        await self.session.flush()


def account_label(account: Account) -> str:
    return (
        account.display_name
        or account.telegram_username
        or account.crmchat_account_id
    )


def normalize_username(value: str) -> str:
    username = value.strip()
    if not username:
        raise ValueError("telegram_username is required")
    return username if username.startswith("@") else f"@{username}"
