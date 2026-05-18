from sqlalchemy import select

from app.models import Dialog
from app.repositories.base import BaseRepository


class DialogRepository(BaseRepository[Dialog]):
    model = Dialog

    async def get_by_crmchat_dialog_id(self, crmchat_dialog_id: str) -> Dialog | None:
        result = await self.session.execute(select(Dialog).where(Dialog.crmchat_dialog_id == crmchat_dialog_id))
        return result.scalar_one_or_none()
