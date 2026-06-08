import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

DIALOGS = {
    "@hunt_pavluck": "cef468a5-1834-4ad1-9479-6e9e4a2c4162",
    "@siloxxx949": "bc144b7e-cd79-4724-bef5-4e3a87f40a22",
}

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    for name, did in DIALOGS.items():
        rows = await c.fetch(
            "SELECT status, count(*) AS n, max(sent_at) AS last_sent, max(updated_at) AS last_upd "
            "FROM outbound_jobs WHERE dialog_id=$1::uuid GROUP BY status ORDER BY status", did)
        blocked = await c.fetch(
            "SELECT error_message FROM outbound_jobs WHERE dialog_id=$1::uuid AND status='blocked' LIMIT 1", did)
        print(f"\n{name}:")
        for r in rows:
            print(f"  {r['status']:10} x{r['n']}  last_sent={r['last_sent']}  last_upd={r['last_upd']}")
        if blocked:
            print(f"  ⚠ blocked reason: {blocked[0]['error_message']}")
    await c.close()

asyncio.run(main())
