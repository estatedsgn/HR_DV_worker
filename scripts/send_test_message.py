from __future__ import annotations

import argparse
import asyncio
import random

from app.services.crmchat_connector import (
    CRMChatAPIError,
    CRMChatConnector,
    build_input_peer_from_resolve_username,
)

DEFAULT_TEXT = "Привет, это тестовое сообщение HR DV Worker."
DEFAULT_USERNAME = "@iamnekiy"

build_peer_from_resolve_username = build_input_peer_from_resolve_username


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a test Telegram message via CRMchat Raw API."
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with CRMChatConnector() as connector:
        ctx = await connector.bootstrap()
        resolved = await connector.resolve_username(
            ctx.workspace.id,
            ctx.telegram_account.id,
            args.username,
        )
        peer = build_peer_from_resolve_username(resolved)

        random_id = str(random.getrandbits(63))
        print(
            f"prepared send workspace={ctx.workspace.id} account={ctx.telegram_account.id} "
            f"username={args.username} random_id={random_id}"
        )
        if args.dry_run:
            print("dry-run: send skipped")
            return

        result = await connector.send_message(
            ctx.workspace.id,
            ctx.telegram_account.id,
            peer,
            args.text,
            random_id,
        )
        print("send result ok", result.get("_", "unknown"))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (CRMChatAPIError, ValueError) as exc:
        print(f"send failed: {exc}")
        raise SystemExit(1) from exc
