import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
ACC2 = "fa819fd6-293e-4ffb-a00c-97df57873896"
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== inbound_events by status (ACC2 dialogs) ===")
    rows = await c.fetch("""
        SELECT ie.status, count(*) n FROM inbound_events ie
        JOIN dialogs d ON d.id=ie.dialog_id WHERE d.account_id=$1 GROUP BY ie.status ORDER BY n DESC
    """, ACC2)
    for r in rows: print(f"  {str(r['status']):12} x{r['n']}")
    print("\n=== ACC2 dialogs: last inbound vs last outbound (lead activity) ===")
    rows = await c.fetch("""
        SELECT d.telegram_username, r.stage,
          (SELECT max(coalesce(m.sent_at,m.created_at)) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') last_in,
          (SELECT max(coalesce(m.sent_at,m.created_at)) FROM messages m WHERE m.dialog_id=d.id AND m.direction='outbound') last_out
        FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
        WHERE d.account_id=$1 AND d.crmchat_dialog_id LIKE 'telegram:%' AND d.telegram_username IS NOT NULL
        ORDER BY last_in DESC NULLS LAST LIMIT 12
    """, ACC2)
    for r in rows:
        li = str(r['last_in'])[:19] if r['last_in'] else '-'
        lo = str(r['last_out'])[:19] if r['last_out'] else '-'
        waiting = "  <-- HER MSG UNANSWERED" if (r['last_in'] and (not r['last_out'] or r['last_in'] > r['last_out'])) else ""
        print(f"  {str(r['telegram_username']):18} stage={str(r['stage']):22} last_in={li} last_out={lo}{waiting}")
    await c.close()
asyncio.run(main())
