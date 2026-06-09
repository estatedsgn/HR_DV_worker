import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.services.crmchat_connector import CRMChatConnector, build_input_peer_from_resolve_username
from sqlalchemy import select
import asyncpg

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "@xxlissxx"
ACCOUNT_ID = sys.argv[2] if len(sys.argv) > 2 else "fa819fd6-293e-4ffb-a00c-97df57873896"

async def main():
    s = get_settings()
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    body = await c.fetchval("""
        SELECT m.body FROM messages m JOIN dialogs d ON d.id=m.dialog_id
        WHERE d.crmchat_dialog_id LIKE 'intake:daivinchik:%' AND lower(d.telegram_username)=lower($1)
        ORDER BY m.created_at LIMIT 1
    """, USERNAME)
    print(f"=== stored opener body for {USERNAME} ===\n  {body!r}\n")
    await c.close()

    async with AsyncSessionLocal() as session:
        acc = (await session.execute(select(Account).where(Account.id==ACCOUNT_ID))).scalar_one_or_none()
    if acc is None:
        print("account not found"); return
    async with CRMChatConnector.for_account(acc) as conn:
        ctx = await conn.bootstrap()
        resolved = await conn.resolve_username(ctx.workspace.id, ctx.telegram_account.id, USERNAME)
        peer = dict(build_input_peer_from_resolve_username(resolved))
        print(f"=== resolved peer {USERNAME}: {peer} ===")
        hist = await conn.get_history(ctx.workspace.id, ctx.telegram_account.id, peer, limit=15)
        msgs = hist.get("messages") if isinstance(hist, dict) else hist
        print(f"\n=== LIVE Telegram history with {USERNAME} (newest first) ===")
        if not msgs:
            print("  <EMPTY — no conversation exists>")
        else:
            for m in (msgs or [])[:15]:
                out = m.get("out")
                txt = (m.get("message") or "")[:60].replace(chr(10)," ")
                print(f"  out={out!s:5} id={m.get('id')} {txt!r}")

asyncio.run(main())
