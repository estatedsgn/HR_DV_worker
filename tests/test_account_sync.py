import asyncio

from app.core.config import Settings
from app.models.account import Account
from app.services.account_sync import AccountSyncService
from app.services.crmchat_connector import (
    CRMChatBootstrapContext,
    CRMChatOrganization,
    CRMChatTelegramAccount,
    CRMChatWorkspace,
)


class FakeAccountRepository:
    existing_by_id = {}
    active = []
    added = []

    def __init__(self, session):
        self.session = session

    async def get_by_crmchat_account_id(self, crmchat_account_id):
        return self.existing_by_id.get(crmchat_account_id)

    async def add(self, instance):
        self.added.append(instance)
        return instance

    async def list_active(self, limit=100):
        return list(self.active)


class FakeSession:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


class FakeConnector:
    async def bootstrap(self):
        return CRMChatBootstrapContext(
            organization=CRMChatOrganization(id="org-1"),
            workspace=CRMChatWorkspace(id="workspace-1"),
            telegram_account=CRMChatTelegramAccount(id="account-1", status="active"),
        )

    async def list_telegram_accounts(self, workspace_id):
        return [
            CRMChatTelegramAccount(
                id="account-1",
                workspace_id=workspace_id,
                status="active",
                username="acc1",
                raw={"id": "account-1"},
            ),
            CRMChatTelegramAccount(
                id="account-2",
                workspace_id=workspace_id,
                status="inactive",
                username="acc2",
                raw={"id": "account-2"},
            ),
        ]


def test_account_sync_dry_run_reports_created_without_commit(monkeypatch):
    import app.services.account_sync as account_sync

    FakeAccountRepository.existing_by_id = {}
    FakeAccountRepository.active = []
    FakeAccountRepository.added = []
    monkeypatch.setattr(account_sync, "AccountRepository", FakeAccountRepository)
    session = FakeSession()
    service = AccountSyncService(
        session,
        connector=FakeConnector(),
        settings=Settings(),
    )

    result = asyncio.run(service.sync_active_accounts(dry_run=True))

    assert result.total_remote == 2
    assert result.active_remote == 1
    assert result.created == 1
    assert session.committed is False
    assert FakeAccountRepository.added == []


def test_account_health_report_counts_statuses(monkeypatch):
    import app.services.account_sync as account_sync

    FakeAccountRepository.existing_by_id = {}
    FakeAccountRepository.active = [
        Account(crmchat_account_id="a1", status="active", health_status="healthy"),
        Account(crmchat_account_id="a2", status="active", health_status="rate_limited"),
        Account(crmchat_account_id="a3", status="active", health_status="unhealthy"),
    ]
    FakeAccountRepository.added = []
    monkeypatch.setattr(account_sync, "AccountRepository", FakeAccountRepository)
    service = AccountSyncService(FakeSession(), connector=FakeConnector(), settings=Settings())

    report = asyncio.run(service.health_report())

    assert report.total == 3
    assert report.healthy == 1
    assert report.rate_limited == 1
    assert report.unhealthy == 1
