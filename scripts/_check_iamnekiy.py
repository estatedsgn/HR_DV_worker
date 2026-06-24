import asyncio, os
from dotenv import load_dotenv
load_dotenv(".env")
import asyncpg


async def main():
    dsn = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    rows = await c.fetch(
        "SELECT m.dialog_id, d.crmchat_dialog_id, m.direction, m.status, "
        "left(m.body,70) b, m.created_at "
        "FROM messages m JOIN dialogs d ON d.id=m.dialog_id "
        "WHERE (d.telegram_username ILIKE 'iamnekiy' OR d.telegram_username ILIKE '@iamnekiy') "
        "AND m.created_at > now() - interval '30 minutes' "
        "ORDER BY m.created_at",
    )
    for r in rows:
        arrow = "->" if r["direction"] == "outbound" else "<-IN"
        print(f"  {r['created_at']:%H:%M:%S} {str(r['dialog_id'])[:8]} {r['crmchat_dialog_id'][:34]:34} {arrow} [{r['status']}] {r['b']}")
    await c.close()


asyncio.run(main())
