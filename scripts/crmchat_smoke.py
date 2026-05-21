from __future__ import annotations

import argparse
import asyncio
import random

from app.core.config import get_settings
from app.services.crmchat_connector import (
    CRMChatAPIError,
    CRMChatConnector,
    build_input_peer_from_resolve_username,
)
from app.services.outbound_queue_worker import normalize_username


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe CRMChat smoke checks.")
    parser.add_argument("--username", default="@iamnekiy")
    parser.add_argument("--send-test", action="store_true")
    parser.add_argument(
        "--text",
        default="Привет, это контролируемый smoke test HR DV Worker.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    allowed = {
        normalize_username(item)
        for item in settings.outbound_allowed_usernames.split(",")
        if item.strip()
    }
    username = normalize_username(args.username)
    if args.send_test and username not in allowed:
        raise ValueError(f"Refusing send: {username} is not in OUTBOUND_ALLOWED_USERNAMES")

    async with CRMChatConnector(settings=settings) as connector:
        context = await connector.bootstrap()
        accounts = await connector.list_telegram_accounts(context.workspace.id)
        active_accounts = [account for account in accounts if account.status == "active"]
        print(
            "crmchat smoke: "
            f"organization={context.organization.id} workspace={context.workspace.id} "
            f"active_accounts={len(active_accounts)} total_accounts={len(accounts)}"
        )

        resolved = await connector.resolve_username(
            context.workspace.id,
            context.telegram_account.id,
            username,
        )
        peer = build_input_peer_from_resolve_username(resolved)
        print(
            "resolve username ok: "
            f"username={username} peer_type={peer.get('_')} has_access_hash={bool(peer.get('accessHash'))}"
        )

        if not args.send_test:
            print("send skipped: pass --send-test for controlled allowlisted send")
            return

        random_id = str(random.getrandbits(63))
        result = await connector.send_message(
            context.workspace.id,
            context.telegram_account.id,
            peer,
            args.text,
            random_id,
        )
        print(f"send ok: username={username} result_type={result.get('_', 'unknown')}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (CRMChatAPIError, ValueError) as exc:
        print(f"crmchat smoke failed: {exc}")
        raise SystemExit(1) from exc
