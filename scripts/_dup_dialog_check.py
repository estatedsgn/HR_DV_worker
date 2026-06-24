import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

DV = ['@mashulya_chit','@angel_12896','@xxlissxx','@Elizabetheyebrow1','@Bular_la','@How_monu','@SofiyaVibes','@rovsyasha','@Raevssskayaa','@vavvchi','@polsheris','@Enooot_22','@springvood','@dasxxh','@persik2200','@sweet08_8','@sfwwffs']

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    print("=== ALL dialogs per daivinchik username (any prefix) + msg counts ===")
    for u in DV:
        rows = await c.fetch("""
            SELECT d.crmchat_dialog_id, d.telegram_peer_id, d.id,
              (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') inb,
              (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='outbound') outb
            FROM dialogs d
            WHERE lower(d.telegram_username)=lower($1)
            ORDER BY d.created_at
        """, u)
        if not rows:
            continue
        line = f"  {u:20}"
        for r in rows:
            pfx = r['crmchat_dialog_id'].split(':')[0]
            peer = 'P' if r['telegram_peer_id'] else '-'
            line += f"  [{pfx} peer={peer} in={r['inb']} out={r['outb']}]"
        print(line)
    print("\n=== telegram: dialogs with inbound, NOT in funnel runtime (deaf replies) ===")
    rows = await c.fetch("""
        SELECT d.telegram_username, d.id,
          (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id AND m.direction='inbound') inb
        FROM dialogs d
        WHERE d.crmchat_dialog_id LIKE 'telegram:%'
          AND lower(coalesce(d.telegram_username,'')) = ANY($1::text[])
    """, [u.lower() for u in DV])
    for r in rows:
        print(f"  {str(r['telegram_username']):20} inbound={r['inb']} dialog={r['id']}")
    await c.close()

asyncio.run(main())
