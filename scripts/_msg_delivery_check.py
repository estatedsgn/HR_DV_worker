import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    cols = [r['column_name'] for r in await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='messages' ORDER BY ordinal_position")]
    print("messages cols:", cols)
    print("\n=== outbound messages on intake:daivinchik dialogs (delivery proof) ===")
    rows = await c.fetch("""
        SELECT d.telegram_username, m.direction, m.status, m.crmchat_message_id, m.created_at
        FROM messages m JOIN dialogs d ON d.id=m.dialog_id
        WHERE d.crmchat_dialog_id LIKE 'intake:daivinchik:%'
        ORDER BY d.telegram_username, m.created_at
    """)
    for r in rows:
        cid = r['crmchat_message_id'] or 'NO-CID'
        print(f"  {str(r['telegram_username']):20} {r['direction']:8} {str(r['status']):8} cid={str(cid):24} {str(r['created_at'])[:19]}")
    await c.close()

asyncio.run(main())
