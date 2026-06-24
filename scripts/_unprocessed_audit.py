import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    # All daivinchik intake events today, newest first, with binding + opener status
    rows = await c.fetch("""
        SELECT ie.telegram_username, ie.created_at, ie.status ie_status,
               d.crmchat_dialog_id, d.telegram_peer_id, d.id dialog_id,
               r.stage,
               (SELECT count(*) FROM outbound_jobs oj WHERE oj.dialog_id=d.id) jobs,
               (SELECT count(*) FROM outbound_jobs oj WHERE oj.dialog_id=d.id AND oj.status='sent') sent,
               (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='outbound') outb
        FROM lead_intake_events ie
        LEFT JOIN dialogs d ON d.id = ie.dialog_id
        LEFT JOIN lead_funnel_runtime r ON r.dialog_id = d.id
        WHERE ie.source='daivinchik' AND ie.created_at > now() - interval '14 hours'
        ORDER BY ie.created_at DESC
    """)
    print(f"{'username':22} {'created(UTC)':17} {'pfx':9} {'peer':5} {'stage':16} jobs/sent/out")
    for r in rows:
        pfx = (r['crmchat_dialog_id'] or 'NONE').split(':')[0]
        peer = 'yes' if r['telegram_peer_id'] else 'NO'
        flag = ''
        if pfx != 'telegram' or (r['sent'] or 0) == 0:
            flag = '  <-- UNPROCESSED'
        print(f"{str(r['telegram_username']):22} {str(r['created_at'])[:16]:17} {pfx:9} {peer:5} {str(r['stage']):16} {r['jobs']}/{r['sent']}/{r['outb']}{flag}")
    await c.close()

asyncio.run(main())
