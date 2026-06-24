"""Re-run the funnel on a dialog's latest inbound so a code change takes effect
on a lead who is already mid-conversation (no new inbound needed).

It calls LangGraphFunnelGateway.decide_for_dialog_message, which runs the graph
on the inbound batch since the last outbound (or the targeted message), persists
the new stage, and enqueues any outbound actions. The already-running autopilot's
OutboundQueueWorker performs the actual send.

    python scripts/reprocess_dialog.py --username @user                 # dry run
    python scripts/reprocess_dialog.py --username @user --match поко --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

sys.stdout.reconfigure(encoding="utf-8")

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.message import Message
from app.services.funnel_graph.gateway import LangGraphFunnelGateway


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username")
    ap.add_argument("--dialog-id")
    ap.add_argument("--match", help="substring of an inbound body to target explicitly")
    ap.add_argument("--apply", action="store_true", help="actually reprocess + enqueue")
    args = ap.parse_args()
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        if args.dialog_id:
            dialog = await session.get(Dialog, args.dialog_id)
        else:
            handle = (args.username or "").lstrip("@").lower()
            rows = (
                await session.execute(
                    select(Dialog)
                    .where(
                        func.lower(func.replace(Dialog.telegram_username, "@", "")) == handle
                    )
                    .order_by(Dialog.created_at.desc())
                )
            ).scalars().all()
            tg = [d for d in rows if str(d.crmchat_dialog_id).startswith("telegram:")]
            dialog = (tg or rows or [None])[0]
        if dialog is None:
            print("no dialog found")
            return

        print(f"dialog id={dialog.id} username={dialog.telegram_username} status={dialog.status}")

        recent = (
            await session.execute(
                select(Message)
                .where(Message.dialog_id == dialog.id)
                .order_by(Message.created_at.desc())
                .limit(8)
            )
        ).scalars().all()
        print("--- last 8 messages (newest first) ---")
        for m in recent:
            print(f"  {m.direction:8} {str(m.created_at)[:19]}  {(m.body or '')[:70]!r}")

        msg_id = None
        if args.match:
            m = (
                await session.execute(
                    select(Message)
                    .where(
                        Message.dialog_id == dialog.id,
                        Message.direction == "inbound",
                        func.lower(Message.body).like(f"%{args.match.lower()}%"),
                    )
                    .order_by(Message.created_at.desc())
                )
            ).scalars().first()
            if m:
                msg_id = str(m.id)
                print(f"--- targeting inbound id={m.id} body={(m.body or '')[:70]!r}")
            else:
                print(f"--- no inbound matched --match={args.match!r}")

        if not args.apply:
            print("DRY RUN — pass --apply to reprocess and enqueue the reply")
            return

        result = await LangGraphFunnelGateway(session, settings=settings).decide_for_dialog_message(
            dialog_id=str(dialog.id), message_id=msg_id
        )
        await session.commit()
        stage = result.get("stage") if result else None
        outs = [
            m.get("text")
            for m in ((result or {}).get("outgoing_messages") or [])
            if m.get("type") == "text"
        ]
        print(f"RESULT stage={stage}")
        print(f"outgoing queued: {outs}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
