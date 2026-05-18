from sqlalchemy import select

from app.models.lead import Lead
from app.repositories.base import BaseRepository


class LeadRepository(BaseRepository[Lead]):
    model = Lead

    async def get_by_dialog(self, dialog_id) -> Lead | None:
        result = await self.session.execute(
            select(Lead).where(Lead.dialog_id == dialog_id)
        )
        return result.scalar_one_or_none()
