"""Hard-reset the funnel for a username and queue a fresh first-touch opener.

Finds the canonical Telegram dialog by username and calls
LangGraphFunnelGateway.restart_for_dialog, which resets the runtime to
interest_check (new thread, cleared state) and queues the opener. The running
autopilot's OutboundQueueWorker performs the actual send (subject to
OUTBOUND_ALLOWED_USERNAMES).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.services.funnel_graph.gateway import LangGraphFunnelGateway


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset a funnel dialog and queue the opener.")
    parser.add_argument("--username")
    parser.add_argument("--dialog-id", help="Restart this exact dialog (use for the canonical telegram dialog).")
    args = parser.parse_args()
    if not args.username and not args.dialog_id:
        parser.error("pass --dialog-id (preferred) or --username")
    return args


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def main() -> None:
    args = parse_args()
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        if args.dialog_id:
            dialog = await session.get(Dialog, args.dialog_id)
            if dialog is None:
                _out(f"ERROR: no dialog found for id={args.dialog_id}")
                raise SystemExit(1)
        else:
            username = args.username if args.username.startswith("@") else f"@{args.username}"
            dialogs = (
                await session.execute(
                    select(Dialog).where(Dialog.telegram_username.ilike(username.lstrip("@")))
                )
            ).scalars().all()
            if not dialogs:
                dialogs = (
                    await session.execute(
                        select(Dialog).where(Dialog.telegram_username.ilike(username))
                    )
                ).scalars().all()
            if not dialogs:
                _out(f"ERROR: no dialog found for username={username}")
                raise SystemExit(1)
            if len(dialogs) > 1:
                _out(f"WARNING: {len(dialogs)} dialogs match {username}; restarting the most recent one")
            dialog = max(dialogs, key=lambda d: d.created_at)
        # Clear any human-review hold so the autopilot can drive the dialog again.
        dialog.status = "active"
        await session.commit()
        dialog_id = str(dialog.id)
        _out(f"restarting dialog id={dialog_id} username={dialog.telegram_username}")

    async with AsyncSessionLocal() as session:
        result = await LangGraphFunnelGateway(session, settings=settings).restart_for_dialog(
            dialog_id=dialog_id
        )
        await session.commit()
        _out(f"restart done stage={result.get('stage') if result else None}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
