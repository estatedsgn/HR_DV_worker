import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
U = "psevdointelllektualka"
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    d = await c.fetchrow("SELECT id FROM dialogs WHERE replace(lower(telegram_username),'@','')=$1 AND crmchat_dialog_id LIKE 'telegram:%'", U)
    did = d['id']
    print("dialog:", did)
    print("\n=== outbound_jobs ===")
    for r in await c.fetch("SELECT id, status, job_type, scheduled_at, left(coalesce(text,''),40) t FROM outbound_jobs WHERE dialog_id=$1 ORDER BY created_at", did):
        print(f"  {str(r['id'])[:8]} {r['status']:10} {r['job_type']:6} sched={str(r['scheduled_at'])[:19]} {r['t']!r}")
    print("\n=== messages ===")
    for r in await c.fetch("SELECT id, direction, status, left(coalesce(body,''),40) b FROM messages WHERE dialog_id=$1 ORDER BY created_at", did):
        print(f"  {str(r['id'])[:8]} {r['direction']:8} {r['status']:10} {r['b']!r}")
    print("\n=== inbound_events ===")
    for r in await c.fetch("SELECT id, status, created_at FROM inbound_events WHERE dialog_id=$1 ORDER BY created_at", did):
        print(f"  {str(r['id'])[:8]} {r['status']:10} {str(r['created_at'])[:19]}")
    print("\n=== lead_funnel_runtime ===")
    for r in await c.fetch("SELECT stage, updated_at FROM lead_funnel_runtime WHERE dialog_id=$1", did):
        print(f"  stage={r['stage']} upd={str(r['updated_at'])[:19]}")
    await c.close()
asyncio.run(main())
