from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.account import Account
from app.repositories.account import AccountRepository
from app.services.crmchat_connector import CRMChatConnector
from app.services.crmchat_diagnostics import redact_value


@dataclass(slots=True, frozen=True)
class AccountSyncResult:
    total_remote: int
    active_remote: int
    created: int
    updated: int


@dataclass(slots=True, frozen=True)
class AccountHealthReport:
    total: int
    active: int
    healthy: int
    rate_limited: int
    unhealthy: int
    next_available_count: int


class AccountSyncService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        connector: CRMChatConnector,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.connector = connector
        self.settings = settings or get_settings()
        self.repository = AccountRepository(session)

    async def sync_active_accounts(self, *, dry_run: bool = False) -> AccountSyncResult:
        context = await self.connector.bootstrap()
        remote_accounts = await self.connector.list_telegram_accounts(context.workspace.id)
        created = updated = 0
        for remote in remote_accounts:
            if remote.status != "active":
                continue
            existing = await self.repository.get_by_crmchat_account_id(remote.id)
            if existing:
                updated += 1
                if not dry_run:
                    apply_remote_account(existing, context.organization.id, context.workspace.id, remote, self.settings)
            else:
                created += 1
                if not dry_run:
                    account = Account(
                        crmchat_account_id=remote.id,
                        crmchat_organization_id=context.organization.id,
                        crmchat_workspace_id=context.workspace.id,
                        telegram_username=remote.username,
                        display_name=remote.username,
                        status="active",
                        health_status="healthy",
                        send_interval_seconds=self.settings.outbound_default_send_interval_seconds,
                        send_jitter_seconds=self.settings.outbound_default_send_jitter_seconds,
                        metadata_json=json.dumps(redact_value(remote.raw or {}), default=str),
                    )
                    await self.repository.add(account)
        if not dry_run:
            await self.session.commit()
        return AccountSyncResult(
            total_remote=len(remote_accounts),
            active_remote=sum(1 for item in remote_accounts if item.status == "active"),
            created=created,
            updated=updated,
        )

    async def reset_expired_rate_limits(self) -> int:
        accounts = await self.repository.list_active(limit=500)
        now = datetime.now(UTC)
        count = 0
        for account in accounts:
            if (
                account.health_status == "rate_limited"
                and account.flood_wait_until is not None
                and account.flood_wait_until <= now
            ):
                account.health_status = "healthy"
                account.flood_wait_until = None
                count += 1
        await self.session.commit()
        return count

    async def health_report(self) -> AccountHealthReport:
        accounts = await self.repository.list_active(limit=500)
        return AccountHealthReport(
            total=len(accounts),
            active=sum(1 for account in accounts if account.status == "active"),
            healthy=sum(1 for account in accounts if account.health_status == "healthy"),
            rate_limited=sum(1 for account in accounts if account.health_status == "rate_limited"),
            unhealthy=sum(1 for account in accounts if account.health_status not in {"healthy", "rate_limited"}),
            next_available_count=sum(1 for account in accounts if account.next_available_at is not None),
        )


def apply_remote_account(account: Account, organization_id: str, workspace_id: str, remote, settings: Settings) -> None:
    account.crmchat_organization_id = organization_id
    account.crmchat_workspace_id = workspace_id
    account.telegram_username = remote.username
    account.display_name = remote.username or account.display_name
    account.status = remote.status or "active"
    if account.health_status == "rate_limited" and account.flood_wait_until:
        if account.flood_wait_until <= datetime.now(UTC):
            account.health_status = "healthy"
            account.flood_wait_until = None
    elif not account.health_status:
        account.health_status = "healthy"
    account.send_interval_seconds = account.send_interval_seconds or settings.outbound_default_send_interval_seconds
    account.send_jitter_seconds = account.send_jitter_seconds or settings.outbound_default_send_jitter_seconds
    account.metadata_json = json.dumps(redact_value(remote.raw or {}), default=str)
