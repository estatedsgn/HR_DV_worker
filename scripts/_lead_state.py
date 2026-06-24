import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
U = (sys.argv[1] if len(sys.argv) > 1 else "psevdointelllektualka").lstrip("@").lower()
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== intake events ===")
    for r in await c.fetch("SELECT source, external_lead_id, telegram_username, status, account_id, created_at FROM lead_intake_events WHERE replace(lower(telegram_username),'@','')=$1 ORDER BY created_at", U):
        print(f"  {r['source']} ext={r['external_lead_id']} user={r['telegram_username']} {r['status']} acct={str(r['account_id'])[:8]} {str(r['created_at'])[:19]}")
    print("\n=== dialogs ===")
    drows = await c.fetch("SELECT id, crmchat_dialog_id, telegram_peer_id, status, created_at FROM dialogs WHERE replace(lower(telegram_username),'@','')=$1 ORDER BY created_at", U)
    for r in drows:
        pfx = r['crmchat_dialog_id'].split(':')[0]; peer = 'yes' if r['telegram_peer_id'] else 'NO'
        st = await c.fetchval("SELECT stage FROM lead_funnel_runtime WHERE dialog_id=$1", r['id'])
        print(f"  [{pfx}] peer={peer} status={r['status']} stage={st} id={r['id']} {str(r['created_at'])[:19]}")
    print("\n=== transcript (all dialogs) ===")
    for r in drows:
        for m in await c.fetch("SELECT direction, status, coalesce(sent_at,created_at) ts, coalesce(body,'') body FROM messages WHERE dialog_id=$1 ORDER BY created_at", r['id']):
            who = "BOT>>" if m['direction']=='outbound' else "her<<"
            print(f"  [{str(m['ts'])[11:19]}] {who} {m['status']:9} {m['body'][:66]}")
    await c.close()
asyncio.run(main())
