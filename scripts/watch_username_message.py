from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.message import Message
from app.services.crmchat_connector import CRMChatConnector
from app.services.telegram_polling import TelegramPollingService, normalize_username


@dataclass(slots=True, frozen=True)
class LatestMessage:
    id: str
    body: str
    sent_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll CRMChat until a new inbound message from one username is persisted."
    )
    parser.add_argument("--username", default="@iamnekiy")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument(
        "--mark-read",
        action="store_true",
        help="Mark inbound messages from this username as read after syncing.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    username = normalize_username(args.username)
    baseline = await latest_inbound_message(username)
    print(
        "watch baseline: "
        f"username={username} latest_id={baseline.id if baseline else None}"
    )

    deadline = asyncio.get_running_loop().time() + args.timeout_seconds
    async with CRMChatConnector() as connector:
        while asyncio.get_running_loop().time() < deadline:
            async with AsyncSessionLocal() as session:
                service = TelegramPollingService(
                    session,
                    connector=connector,
                    only_username=username,
                    mark_read=args.mark_read,
                )
                result = await service.poll_all_active_accounts_once()
                print(
                    "watch poll: "
                    f"dialogs_synced={result.dialogs_synced} "
                    f"messages_seen={result.messages_seen} "
                    f"messages_created={result.messages_created}"
                )
            latest = await latest_inbound_message(username)
            if latest and (baseline is None or latest.id != baseline.id):
                print(
                    "new inbound message persisted: "
                    f"id={latest.id} sent_at={latest.sent_at} body={latest.body}"
                )
                return
            await asyncio.sleep(args.interval_seconds)
    print("watch timeout: no new inbound message persisted")
    raise SystemExit(2)


async def latest_inbound_message(username: str | None) -> LatestMessage | None:
    async with AsyncSessionLocal() as session:
        normalized = (username or "").strip().lower().lstrip("@")
        result = await session.execute(
            select(Message)
            .join(Dialog, Message.dialog_id == Dialog.id)
            .where(
                func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized,
                Message.direction == "inbound",
            )
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        message = result.scalar_one_or_none()
        if message is None:
            return None
        return LatestMessage(
            id=str(message.id),
            body=message.body,
            sent_at=message.sent_at.isoformat() if message.sent_at else "",
        )


if __name__ == "__main__":
    asyncio.run(main())
