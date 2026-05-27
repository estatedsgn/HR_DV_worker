from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.campaign import Campaign, CampaignStep
from app.repositories.base import BaseRepository


class CampaignRepository(BaseRepository[Campaign]):
    model = Campaign

    async def get_default(self) -> Campaign | None:
        result = await self.session.execute(
            select(Campaign)
            .options(selectinload(Campaign.steps))
            .where(Campaign.status == "active", Campaign.is_default.is_(True))
            .order_by(Campaign.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_with_steps(self, campaign_id) -> Campaign | None:
        result = await self.session.execute(
            select(Campaign)
            .options(selectinload(Campaign.steps))
            .where(Campaign.id == campaign_id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_name_with_steps(self, name: str) -> Campaign | None:
        result = await self.session.execute(
            select(Campaign)
            .options(selectinload(Campaign.steps))
            .where(Campaign.name == name)
            .limit(1)
        )
        return result.scalar_one_or_none()


class CampaignStepRepository(BaseRepository[CampaignStep]):
    model = CampaignStep

    async def list_by_campaign(self, campaign_id) -> list[CampaignStep]:
        result = await self.session.execute(
            select(CampaignStep)
            .where(CampaignStep.campaign_id == campaign_id)
            .order_by(CampaignStep.position.asc())
        )
        return list(result.scalars().all())
