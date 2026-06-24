import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg
async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== leads whose LAST message is inbound (unanswered) ===")
    rows = await c.fetch("""
        SELECT d.telegram_username, d.account_id, r.stage,
          (SELECT direction FROM messages m WHERE m.dialog_id=d.id ORDER BY coalesce(m.sent_at,m.created_at) DESC LIMIT 1) last_dir,
          (SELECT max(coalesce(m.sent_at,m.created_at)) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') last_in
        FROM dialogs d LEFT JOIN lead_funnel_runtime r ON r.dialog_id=d.id
        WHERE d.crmchat_dialog_id LIKE 'telegram:%' AND d.telegram_username IS NOT NULL
    """)
    stuck = [r for r in rows if r['last_dir']=='inbound' and r['stage'] not in ('lost','do_not_contact','human_handoff')]
    for r in sorted(stuck, key=lambda r: str(r['last_in'] or ''), reverse=True):
        acct = 'acc1' if str(r['account_id']).startswith('19258606') else 'acc2'
        print(f"  {str(r['telegram_username']):20} {acct} stage={str(r['stage']):24} last_in={str(r['last_in'])[:19]}")
    print(f"\n  total unanswered: {len(stuck)}")
    await c.close()
asyncio.run(main())
