from sqlalchemy import select

from app.models import Account
from app.repositories.base import BaseRepository


class AccountRepository(BaseRepository[Account]):
    model = Account

    async def get_by_crmchat_account_id(self, crmchat_account_id: str) -> Account | None:
        result = await self.session.execute(
            select(Account).where(Account.crmchat_account_id == crmchat_account_id)
        )
        return result.scalar_one_or_none()
