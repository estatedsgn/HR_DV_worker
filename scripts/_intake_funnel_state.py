import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== intake:daivinchik dialogs -> funnel stage + outbound jobs ===")
    rows = await c.fetch("""
        SELECT d.telegram_username, d.id dialog_id, d.telegram_peer_id,
               r.stage, r.updated_at
        FROM dialogs d
        LEFT JOIN lead_funnel_runtime r ON r.dialog_id = d.id
        WHERE d.crmchat_dialog_id LIKE 'intake:daivinchik:%'
        ORDER BY d.created_at DESC
    """)
    for r in rows:
        jobs = await c.fetch("SELECT status, count(*) n FROM outbound_jobs WHERE dialog_id=$1 GROUP BY status", r['dialog_id'])
        jstr = ", ".join(f"{j['status']}:{j['n']}" for j in jobs) or "NO-JOBS"
        peer = r['telegram_peer_id'] or 'NO-PEER'
        print(f"  {str(r['telegram_username']):20} peer={str(peer):10} stage={str(r['stage']):26} jobs=[{jstr}]")
    print("\n=== outbound_jobs for intake dialogs by status (totals) ===")
    rows = await c.fetch("""
        SELECT j.status, count(*) n
        FROM outbound_jobs j JOIN dialogs d ON d.id=j.dialog_id
        WHERE d.crmchat_dialog_id LIKE 'intake:daivinchik:%'
        GROUP BY j.status ORDER BY n DESC
    """)
    for r in rows:
        print(f"  {str(r['status']):14} x{r['n']}")
    await c.close()

asyncio.run(main())
