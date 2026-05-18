from sqlalchemy import select

from app.models import Message
from app.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def list_by_dialog(self, dialog_id, limit: int = 50) -> list[Message]:
        result = await self.session.execute(
            select(Message).where(Message.dialog_id == dialog_id).order_by(Message.sent_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
