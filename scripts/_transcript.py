import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

USER = (sys.argv[1] if len(sys.argv) > 1 else "springvood").lstrip("@").lower()

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    d = await c.fetchrow("""
        SELECT d.id, d.crmchat_dialog_id, r.stage, r.updated_at
        FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
        WHERE replace(lower(d.telegram_username),'@','')=$1 AND d.crmchat_dialog_id LIKE 'telegram:%'
        ORDER BY d.created_at DESC LIMIT 1
    """, USER)
    if not d:
        print("no telegram dialog for", USER); return
    print(f"dialog={d['id']} stage={d['stage']} runtime_upd={d['updated_at']}")
    print("--- messages ordered by created_at (DB processing order) ---")
    rows = await c.fetch("""
        SELECT direction, status, created_at, sent_at, crmchat_message_id cid, coalesce(body,'') body
        FROM messages WHERE dialog_id=$1 ORDER BY created_at
    """, d['id'])
    for r in rows:
        who = "BOT>>" if r['direction']=='outbound' else "her<<"
        cid = (r['cid'] or 'NO-CID')[:14]
        print(f"[{str(r['created_at'])[11:19]}] {who} st={r['status']:9} cid={cid:14} {r['body']}")
    print(f"\ntotal outbound: {sum(1 for r in rows if r['direction']=='outbound')}, inbound: {sum(1 for r in rows if r['direction']=='inbound')}")
    # outbound jobs detail
    print("\n--- outbound_jobs ---")
    jobs = await c.fetch("""SELECT status, attempt_count, created_at, sent_at, left(coalesce(error_message,''),40) err
        FROM outbound_jobs WHERE dialog_id=$1 ORDER BY created_at""", d['id'])
    for j in jobs:
        print(f"  {j['status']:11} att={j['attempt_count']} created={str(j['created_at'])[11:19]} sent={str(j['sent_at'])[11:19] if j['sent_at'] else '-'} {j['err']!r}")
    await c.close()

asyncio.run(main())
