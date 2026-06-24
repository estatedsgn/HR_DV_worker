"""Cold-start a funnel conversation: resolve a Telegram username, create the canonical
dialog (matching what polling would create) and queue the funnel first-touch opener.

The opener is only QUEUED here; the running autopilot's OutboundQueueWorker performs the
actual send (subject to OUTBOUND_ALLOWED_USERNAMES).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.models.dialog import Dialog
from app.services.crmchat_connector import CRMChatConnector, build_input_peer_from_resolve_username
from app.services.funnel_graph.gateway import LangGraphFunnelGateway


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cold-start a funnel dialog for a username.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--resolve-only", action="store_true", help="Only resolve the username; do not create or send.")
    return parser.parse_args()


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def main() -> None:
    args = parse_args()
    username = args.username if args.username.startswith("@") else f"@{args.username}"
    settings = get_settings()

    async with CRMChatConnector() as connector:
        ctx = await connector.bootstrap()
        crmchat_account_id = ctx.telegram_account.id
        resolved = await connector.resolve_username(ctx.workspace.id, crmchat_account_id, username)
        peer = dict(build_input_peer_from_resolve_username(resolved))
        _out(f"resolved username={username} peer={peer}")

        user_id = str(peer.get("userId") or peer.get("user_id") or "")
        access_hash = peer.get("accessHash") or peer.get("access_hash")
        if not user_id:
            _out("ERROR: could not extract user id from resolve result")
            raise SystemExit(1)
        dialog_external_id = f"telegram:{crmchat_account_id}:user:{user_id}"
        _out(f"dialog_external_id={dialog_external_id}")

        if args.resolve_only:
            return

        async with AsyncSessionLocal() as session:
            acc = (
                await session.execute(select(Account).where(Account.crmchat_account_id == crmchat_account_id))
            ).scalar_one_or_none()
            if acc is None:
                acc = (await session.execute(select(Account))).scalars().first()
            dlg = (
                await session.execute(select(Dialog).where(Dialog.crmchat_dialog_id == dialog_external_id))
            ).scalar_one_or_none()
            if dlg is None:
                dlg = Dialog(
                    account_id=acc.id,
                    crmchat_dialog_id=dialog_external_id,
                    lead_external_id=user_id,
                    telegram_peer_type="user",
                    telegram_peer_id=user_id,
                    telegram_access_hash=str(access_hash) if access_hash is not None else None,
                    telegram_username=username,
                    status="active",
                )
                session.add(dlg)
                await session.flush()
                _out(f"created dialog id={dlg.id}")
            else:
                dlg.telegram_peer_type = "user"
                dlg.telegram_peer_id = user_id
                dlg.telegram_access_hash = str(access_hash) if access_hash is not None else dlg.telegram_access_hash
                dlg.telegram_username = username
                _out(f"reusing dialog id={dlg.id}")
            dialog_id = str(dlg.id)
            await session.commit()

        async with AsyncSessionLocal() as session:
            result = await LangGraphFunnelGateway(session, settings=settings).start_for_dialog(dialog_id=dialog_id)
            await session.commit()
            _out(f"start_for_dialog stage={result.get('stage') if result else None}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
