from sqlalchemy import select

from app.models.message import Message
from app.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    model = Message

    async def get_by_crmchat_message_id(
        self, crmchat_message_id: str
    ) -> Message | None:
        result = await self.session.execute(
            select(Message).where(Message.crmchat_message_id == crmchat_message_id)
        )
        return result.scalar_one_or_none()

    async def adopt_outbound_readback(
        self, dialog_id, body: str, crmchat_message_id: str
    ) -> Message | None:
        """Сопоставить перечитанное поллингом НАШЕ исходящее с уже существующей
        записью этого же сообщения, созданной при отправке.

        Каждое исходящее, что мы шлём, кладётся в БД дважды: запись воркера при
        отправке (без crmchat_message_id) и затем перечитанная поллингом копия. Из-за
        второй копии бот видит своё сообщение в истории дважды и «извиняется, что
        написал дважды». Здесь мы вместо вставки второй копии «усыновляем» исходную
        запись: проставляем ей crmchat_message_id, чтобы дубль больше не создавался.

        Берём самую раннюю ещё не привязанную исходящую запись с тем же текстом
        (FIFO — сопоставляем перечитки по порядку отправки).
        """
        result = await self.session.execute(
            select(Message)
            .where(
                Message.dialog_id == dialog_id,
                Message.direction == "outbound",
                Message.crmchat_message_id.is_(None),
                Message.body == body,
            )
            .order_by(Message.created_at.asc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            return None
        existing.crmchat_message_id = crmchat_message_id
        return existing

    async def list_by_dialog(self, dialog_id, limit: int = 50) -> list[Message]:
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.sent_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
