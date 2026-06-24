"""Собрать юзернеймы активных (не ушедших в lost/do_not_contact) лидов воронки."""
import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

TERMINAL = ("lost", "do_not_contact")

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    rows = await c.fetch(
        """
        SELECT d.telegram_username, r.stage, r.updated_at
        FROM lead_funnel_runtime r
        JOIN dialogs d ON d.id = r.dialog_id
        WHERE d.crmchat_dialog_id LIKE 'telegram:%'
          AND d.telegram_username IS NOT NULL
        ORDER BY r.updated_at DESC
        """
    )
    active = []
    print("=== funnel runtimes (telegram dialogs) ===")
    seen = set()
    for r in rows:
        u = (r["telegram_username"] or "").strip()
        mark = "LOST" if r["stage"] in TERMINAL else "active"
        print(f"  {u:24} stage={r['stage']:24} {mark}  upd={str(r['updated_at'])[:19]}")
        if r["stage"] not in TERMINAL and u and u.lower() not in seen:
            active.append(u)
            seen.add(u.lower())
    print("\nACTIVE_USERNAMES=" + ",".join(active))
    await c.close()

asyncio.run(main())
