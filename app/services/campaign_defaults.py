from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import Campaign, CampaignStep
from app.repositories.campaign import CampaignRepository


DEFAULT_CAMPAIGN_NAME = "default-v1"


class DefaultCampaignService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def ensure_default_campaign(self) -> Campaign:
        existing = await CampaignRepository(self.session).get_default()
        if existing:
            return existing

        campaign = Campaign(
            name=DEFAULT_CAMPAIGN_NAME,
            description="Default V1 deterministic ladder before LLM handling.",
            status="active",
            is_default=True,
            metadata_json={"version": 1},
        )
        self.session.add(campaign)
        await self.session.flush()
        self.session.add_all(
            [
                CampaignStep(
                    campaign_id=campaign.id,
                    position=1,
                    step_type="fixed_message",
                    message_text="Здравствуйте! Пишу по поводу сотрудничества. Удобно обсудить детали?",
                    delay_seconds=0,
                    wait_for_reply=True,
                    metadata_json={},
                ),
                CampaignStep(
                    campaign_id=campaign.id,
                    position=2,
                    step_type="fixed_message",
                    message_text="Спасибо за ответ. Подскажите, пожалуйста, какой формат сотрудничества вам интересен?",
                    delay_seconds=0,
                    wait_for_reply=False,
                    metadata_json={},
                ),
                CampaignStep(
                    campaign_id=campaign.id,
                    position=3,
                    step_type="fixed_message",
                    message_text="Могу кратко зафиксировать детали и передать их менеджеру для следующего шага.",
                    delay_seconds=3600,
                    wait_for_reply=True,
                    metadata_json={},
                ),
                CampaignStep(
                    campaign_id=campaign.id,
                    position=4,
                    step_type="llm_decision",
                    delay_seconds=0,
                    wait_for_reply=False,
                    metadata_json={},
                ),
            ]
        )
        await self.session.flush()
        return campaign
