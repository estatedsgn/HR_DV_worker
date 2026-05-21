from __future__ import annotations

from sqlalchemy import select

from app.models.lead_intake_event import LeadIntakeEvent
from app.repositories.base import BaseRepository


class LeadIntakeEventRepository(BaseRepository[LeadIntakeEvent]):
    model = LeadIntakeEvent

    async def get_duplicate(self, *, source: str, external_lead_id: str) -> LeadIntakeEvent | None:
        result = await self.session.execute(
            select(LeadIntakeEvent)
            .where(
                LeadIntakeEvent.source == source,
                LeadIntakeEvent.external_lead_id == external_lead_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()
