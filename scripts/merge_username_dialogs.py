from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.message import Message


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Merge duplicate Telegram polling dialogs into an intake dialog for one username."
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    normalized = args.username.strip().lower().lstrip("@")
    async with AsyncSessionLocal() as session:
        dialogs = list(
            (
                await session.execute(
                    select(Dialog)
                    .where(func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized)
                    .order_by(Dialog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        intake = next((dialog for dialog in dialogs if dialog.crmchat_dialog_id.startswith("intake:")), None)
        if intake is None:
            print(f"merge skipped: no intake dialog for {args.username}")
            return
        duplicates = [dialog for dialog in dialogs if dialog.id != intake.id]
        moved = 0
        for duplicate in duplicates:
            if duplicate.telegram_peer_type and not intake.telegram_peer_type:
                intake.telegram_peer_type = duplicate.telegram_peer_type
                intake.telegram_peer_id = duplicate.telegram_peer_id
                intake.telegram_access_hash = duplicate.telegram_access_hash
                intake.telegram_username = duplicate.telegram_username
            messages = list(
                (
                    await session.execute(
                        select(Message).where(Message.dialog_id == duplicate.id)
                    )
                )
                .scalars()
                .all()
            )
            for message in messages:
                moved += 1
                if not args.dry_run:
                    message.dialog_id = intake.id
        if not args.dry_run:
            await session.commit()
        print(
            "merge result: "
            f"username={args.username} intake_dialog={intake.id} "
            f"duplicates={len(duplicates)} moved_messages={moved} dry_run={args.dry_run}"
        )


if __name__ == "__main__":
    asyncio.run(main())
