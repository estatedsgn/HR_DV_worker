"""Unit coverage for the per-account (multi-account) wiring.

These exercise the pure logic added for running many accounts in parallel, each
with its own CRMchat key, Дайвинчик state and funnel scope — no DB required.
"""

from types import SimpleNamespace

import pytest

import app.services.lead_intake as lead_intake
from app.services.crmchat_connector import CRMChatConnector
from app.services.daivinchik.service import (
    DaivinchikService,
    _account_slug,
    _suffixed_path,
)


def test_connector_for_account_uses_account_credentials():
    account = SimpleNamespace(
        crmchat_api_base_url="https://acct.example",
        crmchat_api_key="acct-key",
    )
    settings = SimpleNamespace(
        crmchat_api_base_url="https://global.example",
        crmchat_api_key="global-key",
        crmchat_timeout_seconds=60.0,
    )
    conn = CRMChatConnector.for_account(account, settings=settings)
    assert str(conn.http_client.base_url).startswith("https://acct.example")
    assert conn.http_client.headers["Authorization"] == "Bearer acct-key"


def test_connector_for_account_falls_back_to_settings():
    account = SimpleNamespace(crmchat_api_base_url=None, crmchat_api_key=None)
    settings = SimpleNamespace(
        crmchat_api_base_url="https://global.example",
        crmchat_api_key="global-key",
        crmchat_timeout_seconds=60.0,
    )
    conn = CRMChatConnector.for_account(account, settings=settings)
    assert str(conn.http_client.base_url).startswith("https://global.example")
    assert conn.http_client.headers["Authorization"] == "Bearer global-key"


def test_suffixed_path_isolates_per_account():
    assert _suffixed_path("daivinchik_state.json", "acc2") == "daivinchik_state_acc2.json"
    # no suffix => legacy single-account filename unchanged
    assert _suffixed_path("daivinchik_state.json", None) == "daivinchik_state.json"


def test_account_slug_sanitizes_username():
    account = SimpleNamespace(telegram_username="@Iam_Nekiy", crmchat_account_id="c1", id="u1")
    assert _account_slug(account) == "Iam_Nekiy"


def test_daivinchik_service_per_account_state_and_config():
    account = SimpleNamespace(
        telegram_username="@acc2",
        crmchat_account_id="c2",
        id="u2",
        crmchat_api_base_url=None,
        crmchat_api_key=None,
        daivinchik_config_json='{"daivinchik_like_probability": 0.9, "daily_lead_limit": 3}',
    )
    service = DaivinchikService(account=account)

    # Per-account state file so two accounts never share a swipe cursor.
    assert str(service.state.path).endswith("daivinchik_state_acc2.json")
    assert str(service.leads_path).endswith("daivinchik_leads_acc2.jsonl")

    # Long-key override wins; short-key override wins; everything else falls back.
    assert service.cfg.daivinchik_like_probability == 0.9
    assert service.cfg.daivinchik_daily_lead_limit == 3
    assert service.cfg.daivinchik_bot_username  # default from settings


def test_daivinchik_service_default_is_single_account():
    service = DaivinchikService()
    assert str(service.state.path).endswith("daivinchik_state.json")
    assert "daivinchik_state_" not in str(service.state.path)


class _FakeAccountRepo:
    def __init__(self, session):
        pass

    async def get(self, object_id):
        if object_id == "known":
            return SimpleNamespace(id="known")
        return None

    async def get_available_for_assignment(self):
        return SimpleNamespace(id="fallback")


@pytest.mark.asyncio
async def test_select_account_binds_to_given_account(monkeypatch):
    monkeypatch.setattr(lead_intake, "AccountRepository", _FakeAccountRepo)
    service = lead_intake.LeadIntakeService(
        session=SimpleNamespace(), notifier=SimpleNamespace()
    )

    # Explicit account id => bind to it (the account that matched the lead).
    bound = await service._select_account("known")
    assert bound.id == "known"

    # No id => legacy "next available" behaviour.
    fallback = await service._select_account(None)
    assert fallback.id == "fallback"

    # Unknown id => fall back rather than crash.
    missing = await service._select_account("missing")
    assert missing.id == "fallback"
