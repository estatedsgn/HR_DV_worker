import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
ACC2 = "fa819fd6-293e-4ffb-a00c-97df57873896"
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== ACC2 intake events (last 6) ===")
    for r in await c.fetch("""SELECT telegram_username, status, created_at FROM lead_intake_events
        WHERE account_id=$1 ORDER BY created_at DESC LIMIT 6""", ACC2):
        print(f"  {str(r['telegram_username']):20} {r['status']:10} {str(r['created_at'])[:19]}")
    print("\n=== ACC2 telegram dialogs by recency (last 6) + first/last msg ===")
    rows = await c.fetch("""SELECT d.id, d.telegram_username, d.created_at, r.stage,
        (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') inb,
        (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='outbound') outb,
        (SELECT min(coalesce(m.sent_at,m.created_at)) FROM messages m WHERE m.dialog_id=d.id) firstmsg
        FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
        WHERE d.account_id=$1 AND d.crmchat_dialog_id LIKE 'telegram:%'
        ORDER BY d.created_at DESC LIMIT 6""", ACC2)
    for r in rows:
        print(f"  {str(r['telegram_username']):20} created={str(r['created_at'])[:19]} stage={str(r['stage']):16} in={r['inb']} out={r['outb']}")
        # show transcript of the newest one
    if rows:
        newest = rows[0]
        print(f"\n=== transcript newest: {newest['telegram_username']} ===")
        for m in await c.fetch("""SELECT direction, status, coalesce(sent_at,created_at) ts, coalesce(body,'') body
            FROM messages WHERE dialog_id=$1 ORDER BY created_at""", newest['id']):
            who = "BOT>>" if m['direction']=='outbound' else "her<<"
            print(f"  [{str(m['ts'])[11:19]}] {who} {m['status']:9} {m['body'][:70]}")
    await c.close()
asyncio.run(main())
