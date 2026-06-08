from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, nullsfirst, or_, select

from app.models.account import Account
from app.repositories.base import BaseRepository


class AccountRepository(BaseRepository[Account]):
    model = Account

    async def get_by_crmchat_account_id(
        self, crmchat_account_id: str
    ) -> Account | None:
        result = await self.session.execute(
            select(Account).where(Account.crmchat_account_id == crmchat_account_id)
        )
        return result.scalar_one_or_none()

    async def get_by_reference(self, ref: str) -> Account | None:
        """Resolve an account from a CLI/config reference: a UUID id, a CRMchat
        account id, or a @telegram_username (case/@-insensitive)."""
        ref = ref.strip()
        if not ref:
            return None
        try:
            uid = UUID(ref)
        except (ValueError, TypeError):
            uid = None
        if uid is not None:
            account = await self.session.get(Account, uid)
            if account is not None:
                return account
        account = await self.get_by_crmchat_account_id(ref)
        if account is not None:
            return account
        normalized = ref.lstrip("@").lower()
        result = await self.session.execute(
            select(Account).where(
                func.lower(func.replace(Account.telegram_username, "@", "")) == normalized
            )
        )
        return result.scalar_one_or_none()

    async def list_daivinchik_enabled(self, limit: int = 200) -> list[Account]:
        """Active accounts that should run a Дайвинчик swiper + funnel process."""
        result = await self.session.execute(
            select(Account)
            .where(Account.status == "active", Account.daivinchik_enabled.is_(True))
            .order_by(Account.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_available_for_assignment(self) -> Account | None:
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(Account)
            .where(
                Account.status == "active",
                Account.health_status == "healthy",
                or_(Account.flood_wait_until.is_(None), Account.flood_wait_until <= now),
            )
            .order_by(nullsfirst(Account.next_available_at.asc()), Account.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_active(self, limit: int = 100) -> list[Account]:
        result = await self.session.execute(
            select(Account)
            .where(Account.status == "active")
            .order_by(Account.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())
