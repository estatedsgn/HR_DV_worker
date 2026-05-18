from sqlalchemy import select

from app.models.crmchat_webhook_event import CRMChatWebhookEvent
from app.repositories.base import BaseRepository


class CRMChatWebhookEventRepository(BaseRepository[CRMChatWebhookEvent]):
    model = CRMChatWebhookEvent

    async def get_by_event_id(self, event_id: str) -> CRMChatWebhookEvent | None:
        result = await self.session.execute(
            select(CRMChatWebhookEvent).where(CRMChatWebhookEvent.event_id == event_id)
        )
        return result.scalar_one_or_none()
