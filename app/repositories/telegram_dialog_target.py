from sqlalchemy import select

from app.models.telegram_dialog_target import TelegramDialogTarget
from app.repositories.base import BaseRepository


class TelegramDialogTargetRepository(BaseRepository[TelegramDialogTarget]):
    model = TelegramDialogTarget

    async def is_target(self, *, account_id, peer_type: str, peer_id: str) -> bool:
        result = await self.session.execute(
            select(TelegramDialogTarget.id).where(
                TelegramDialogTarget.account_id == account_id,
                TelegramDialogTarget.telegram_peer_type == peer_type,
                TelegramDialogTarget.telegram_peer_id == peer_id,
                TelegramDialogTarget.is_active.is_(True),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None
