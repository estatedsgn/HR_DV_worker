from __future__ import annotations

import argparse
import asyncio
import random
from typing import Any, Mapping

from app.services.crmchat_connector import CRMChatAPIError, CRMChatConnector

DEFAULT_TEXT = "Привет мой дорогой"
DEFAULT_USERNAME = "@iamnekiy"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a test Telegram message via CRMchat Raw API."
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def build_peer_from_resolve_username(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(data, Mapping):
        raise ValueError("resolveUsername payload must be an object")

    peer = data.get("peer")
    if isinstance(peer, Mapping):
        peer_type = str(peer.get("_") or "").lower()
        if "peeruser" in peer_type and peer.get("userId") is not None:
            user_id = int(peer["userId"])
            access_hash = _find_access_hash_for_user(data, user_id)
            if access_hash is None:
                raise ValueError("Unable to find accessHash for resolved user")
            return {
                "_": "inputPeerUser",
                "userId": int(user_id),
                "accessHash": str(access_hash),
            }

    # fallback: direct user object
    users = data.get("users") if isinstance(data.get("users"), list) else []
    if users and isinstance(users[0], Mapping):
        user = users[0]
        if user.get("id") is None or user.get("accessHash") is None:
            raise ValueError("Resolved user payload missing id/accessHash")
        return {
            "_": "inputPeerUser",
            "userId": int(user["id"]),
            "accessHash": str(user["accessHash"]),
        }

    raise ValueError("Could not build inputPeerUser from resolveUsername payload")


def _find_access_hash_for_user(data: Mapping[str, Any], user_id: int) -> str | None:
    users = data.get("users") if isinstance(data.get("users"), list) else []
    for raw_user in users:
        if not isinstance(raw_user, Mapping):
            continue
        if int(raw_user.get("id", -1)) == user_id and raw_user.get("accessHash") is not None:
            return str(raw_user["accessHash"])
    return None


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
