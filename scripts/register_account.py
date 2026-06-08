"""Зарегистрировать (или обновить) аккаунт со своим CRMchat-ключом в БД.

Каждый Дайвинчик-аккаунт — отдельное подключение CRMchat со своим Bearer-ключом.
Этот скрипт принимает ключ, через bootstrap узнаёт org/workspace/telegram-account
и @username, и складывает всё в строку accounts. Ключ нигде не коммитится —
он живёт только в БД (и в аргументе запуска).

Примеры:
    .venv\\Scripts\\python.exe scripts\\register_account.py --api-key sk_xxx --daivinchik
    .venv\\Scripts\\python.exe scripts\\register_account.py --api-key sk_xxx \\
        --base-url https://api.crmchat.ai --daivinchik \\
        --config-json "{\\"daivinchik_like_probability\\": 0.6, \\"daivinchik_daily_lead_limit\\": 5}"

Безопаснее передавать ключ через переменную окружения, чтобы он не попал в историю
shell:
    $env:NEW_ACCOUNT_KEY = "sk_xxx"; .venv\\Scripts\\python.exe scripts\\register_account.py --daivinchik
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.repositories.account import AccountRepository
from app.services.crmchat_connector import CRMChatConnector


def _mask(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register a per-account CRMchat key")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("NEW_ACCOUNT_KEY"),
        help="CRMchat bearer key for this account (or env NEW_ACCOUNT_KEY).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="CRMchat API base url. Defaults to the global CRMCHAT_API_BASE_URL.",
    )
    parser.add_argument(
        "--daivinchik",
        action="store_true",
        help="Enable the Дайвинчик swiper for this account (orchestrator will spawn it).",
    )
    parser.add_argument(
        "--config-json",
        default=None,
        help="Inline JSON with per-account daivinchik_* overrides (its «режим»).",
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Optional human label for the account.",
    )
    return parser.parse_args()


async def _amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    api_key = (args.api_key or "").strip()
    if not api_key:
        print("register_account: --api-key (or env NEW_ACCOUNT_KEY) is required")
        return 2
    base_url = args.base_url or settings.crmchat_api_base_url
    if not base_url:
        print("register_account: no base url (pass --base-url or set CRMCHAT_API_BASE_URL)")
        return 2

    config_json: str | None = None
    if args.config_json:
        try:
            json.loads(args.config_json)  # validate
        except ValueError as exc:
            print(f"register_account: --config-json is not valid JSON: {exc}")
            return 2
        config_json = args.config_json

    connector = CRMChatConnector(settings=settings, base_url=base_url, api_key=api_key)
    try:
        context = await connector.bootstrap()
    finally:
        await connector.aclose()

    org = context.organization
    workspace = context.workspace
    telegram = context.telegram_account
    print(
        f"resolved: org={org.id} workspace={workspace.id} "
        f"telegram_account={telegram.id} username={telegram.username} "
        f"key={_mask(api_key)}"
    )

    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_crmchat_account_id(telegram.id)
        created = account is None
        if account is None:
            from app.models.account import Account

            account = Account(crmchat_account_id=telegram.id)
            session.add(account)
        account.crmchat_organization_id = org.id
        account.crmchat_workspace_id = workspace.id
        account.crmchat_api_base_url = base_url
        account.crmchat_api_key = api_key
        account.telegram_username = telegram.username or account.telegram_username
        account.display_name = (
            args.display_name or account.display_name or telegram.username
        )
        account.status = "active"
        account.health_status = "healthy"
        account.daivinchik_enabled = bool(args.daivinchik)
        if config_json is not None:
            account.daivinchik_config_json = config_json
        if not account.send_interval_seconds:
            account.send_interval_seconds = settings.outbound_default_send_interval_seconds
        if not account.send_jitter_seconds:
            account.send_jitter_seconds = settings.outbound_default_send_jitter_seconds
        await session.commit()
        account_id = str(account.id)

    print(
        f"{'created' if created else 'updated'} account id={account_id} "
        f"daivinchik={'on' if args.daivinchik else 'off'}"
    )
    print(f"\nRun its process pair with:\n  daivinchik: python -m app.services.daivinchik --account {account_id}\n"
          f"  autopilot:  python scripts/run_autopilot.py --account {account_id} --leads-only --allow-real-send")
    return 0


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = _parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
