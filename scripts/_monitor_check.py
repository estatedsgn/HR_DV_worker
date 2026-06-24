import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

WATCH = ['leader_elayna','angel_12896','xxlissxx','mashulya_chit','Bular_la','How_monu','SofiyaVibes','rovsyasha']

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== watch leads (telegram dialogs) ===")
    for u in WATCH:
        d = await c.fetchrow("""
            SELECT d.id, r.stage
            FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
            WHERE replace(lower(d.telegram_username),'@','')=lower($1) AND d.crmchat_dialog_id LIKE 'telegram:%'
            ORDER BY d.created_at DESC LIMIT 1
        """, u)
        if not d:
            print(f"  {u:18} <no telegram dialog>"); continue
        jobs = await c.fetch("SELECT status, count(*) n FROM outbound_jobs WHERE dialog_id=$1 GROUP BY status", d['id'])
        jstr = ", ".join(f"{j['status']}:{j['n']}" for j in jobs) or "NO-JOBS"
        inb = await c.fetchval("SELECT count(*) FROM messages WHERE dialog_id=$1 AND direction='inbound'", d['id'])
        outb = await c.fetchval("SELECT count(*) FROM messages WHERE dialog_id=$1 AND direction='outbound' AND status='sent'", d['id'])
        print(f"  {u:18} stage={str(d['stage']):20} jobs=[{jstr}] inbound={inb} sent_out={outb}")
    print("\n=== outbound_jobs global by status (now) ===")
    for r in await c.fetch("SELECT status, count(*) n FROM outbound_jobs GROUP BY status ORDER BY n DESC"):
        print(f"  {str(r['status']):12} x{r['n']}")
    await c.close()

asyncio.run(main())
