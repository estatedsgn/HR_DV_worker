from sqlalchemy import case, func, select

from app.models.dialog import Dialog
from app.repositories.base import BaseRepository


class DialogRepository(BaseRepository[Dialog]):
    model = Dialog

    async def get_by_crmchat_dialog_id(self, crmchat_dialog_id: str) -> Dialog | None:
        result = await self.session.execute(
            select(Dialog).where(Dialog.crmchat_dialog_id == crmchat_dialog_id)
        )
        return result.scalar_one_or_none()

    async def get_by_telegram_peer(self, peer_type: str, peer_id: str) -> Dialog | None:
        result = await self.session.execute(
            select(Dialog).where(
                Dialog.telegram_peer_type == peer_type,
                Dialog.telegram_peer_id == peer_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_telegram_username(self, username: str) -> Dialog | None:
        normalized = normalize_username(username)
        result = await self.session.execute(
            select(Dialog)
            .where(func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized)
            .order_by(
                case((Dialog.crmchat_dialog_id.like("intake:%"), 0), else_=1),
                Dialog.created_at.asc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


def normalize_username(username: str) -> str:
    return username.strip().lower().lstrip("@")
