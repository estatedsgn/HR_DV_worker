"""Dump raw getHistory messages to inspect whether CRMChat exposes reply context.

    python scripts/_raw_history_probe.py durabi2li5 db850ae7-d8dc-4618-a038-8957dd9dd616
"""
import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from sqlalchemy import select  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.services.crmchat_connector import (  # noqa: E402
    CRMChatConnector,
    build_input_peer_from_resolve_username,
)

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "durabi2li5"
ACCOUNT_ID = sys.argv[2] if len(sys.argv) > 2 else "db850ae7-d8dc-4618-a038-8957dd9dd616"


async def main() -> None:
    async with AsyncSessionLocal() as session:
        acc = (await session.execute(select(Account).where(Account.id == ACCOUNT_ID))).scalar_one_or_none()
    if acc is None:
        print("account not found"); return
    async with CRMChatConnector.for_account(acc) as conn:
        ctx = await conn.bootstrap()
        resolved = await conn.resolve_username(ctx.workspace.id, ctx.telegram_account.id, USERNAME)
        peer = dict(build_input_peer_from_resolve_username(resolved))
        hist = await conn.get_history(ctx.workspace.id, ctx.telegram_account.id, peer, limit=12)
        msgs = hist.get("messages") if isinstance(hist, dict) else hist
        print(f"=== {len(msgs or [])} raw messages for {USERNAME} ===")
        for m in (msgs or [])[:12]:
            keys = sorted(m.keys()) if isinstance(m, dict) else []
            reply = m.get("replyTo") or m.get("reply_to") if isinstance(m, dict) else None
            print("-" * 60)
            print(f"id={m.get('id')} out={m.get('out')} keys={keys}")
            print(f"  text={(m.get('message') or '')[:50]!r}")
            print(f"  replyTo={json.dumps(reply, ensure_ascii=False) if reply else None}")


if __name__ == "__main__":
    asyncio.run(main())
