import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== dialogs by crmchat_dialog_id prefix ===")
    for r in await c.fetch("SELECT split_part(crmchat_dialog_id,':',1) pfx, count(*) n FROM dialogs GROUP BY pfx ORDER BY n DESC"):
        print(f"  {str(r['pfx']):20} x{r['n']}")
    q_total = "SELECT count(*) FROM dialogs WHERE crmchat_dialog_id LIKE 'intake:%'"
    q_peer = "SELECT count(*) FROM dialogs WHERE crmchat_dialog_id LIKE 'intake:%' AND telegram_peer_id IS NOT NULL"
    print("\n  total intake dialogs =", await c.fetchval(q_total), "| with peer =", await c.fetchval(q_peer))
    print("\n=== intake placeholder dialogs (recent 40) ===")
    rows = await c.fetch("SELECT telegram_username, crmchat_dialog_id, telegram_peer_id, created_at FROM dialogs WHERE crmchat_dialog_id LIKE 'intake:%' ORDER BY created_at DESC LIMIT 40")
    for r in rows:
        peer = r['telegram_peer_id'] or 'NO-PEER'
        print(f"  {str(r['telegram_username']):22} peer={str(peer):12} {str(r['created_at'])[:19]} {r['crmchat_dialog_id']}")
    print("\n=== lead_intake_events cols + recent ===")
    cols = [r['column_name'] for r in await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='lead_intake_events' ORDER BY ordinal_position")]
    print("  cols:", cols)
    rows = await c.fetch("SELECT * FROM lead_intake_events ORDER BY created_at DESC LIMIT 12")
    for r in rows:
        print("  ", {k: str(v)[:28] for k,v in dict(r).items()})
    await c.close()

asyncio.run(main())
