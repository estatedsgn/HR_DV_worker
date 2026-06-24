import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    # Real lead dialogs: telegram:, has a username, NOT the *_crm demo junk, with >=1 inbound
    rows = await c.fetch("""
        SELECT d.id, d.telegram_username, d.account_id, r.stage,
          (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') inb
        FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
        WHERE d.crmchat_dialog_id LIKE 'telegram:%' AND d.telegram_username IS NOT NULL
          AND d.telegram_username NOT LIKE '%\_crm'
        ORDER BY r.updated_at DESC NULLS LAST
    """)
    real = [r for r in rows if (r['inb'] or 0) >= 1]
    print(f"# {len(real)} lead dialogs with inbound\n")
    for r in real:
        acct = '1' if str(r['account_id']).startswith('19258606') else '2'
        print(f"===== @{r['telegram_username']} | acc{acct} | stage={r['stage']} =====")
        msgs = await c.fetch("""SELECT direction, status, coalesce(sent_at,created_at) ts, coalesce(body,'') body
            FROM messages WHERE dialog_id=$1 AND status<>'synced' ORDER BY created_at""", r['id'])
        for m in msgs:
            who = ">>" if m['direction']=='outbound' else "<<"
            b = m['body'][:90] if m['body'] else ('[ПУСТО]' if m['direction']=='outbound' else '[media]')
            print(f"  {who} {b}")
        print()
    await c.close()
asyncio.run(main())
