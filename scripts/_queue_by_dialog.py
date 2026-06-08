import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

HUNT = "cef468a5-1834-4ad1-9479-6e9e4a2c4162"
SILO = "bc144b7e-cd79-4724-bef5-4e3a87f40a22"

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    cols = [r["column_name"] for r in await c.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1", "inbound_events")]
    print("inbound_events cols:", cols)
    dcol = "dialog_id" if "dialog_id" in cols else None
    print(f"\n=== queued inbound_events grouped by dialog (dcol={dcol}) ===")
    if dcol:
        rows = await c.fetch(
            f"SELECT e.{dcol} AS did, d.telegram_username AS uname, count(*) AS n "
            f"FROM inbound_events e LEFT JOIN dialogs d ON d.id = e.{dcol} "
            "WHERE e.status='queued' GROUP BY e.{0}, d.telegram_username ORDER BY n DESC".format(dcol))
        for r in rows:
            tag = "<<< HUNT" if str(r["did"]) == HUNT else ("<<< SILO" if str(r["did"]) == SILO else "")
            print(f"  {r['uname']!s:20} {r['did']}  x{r['n']}  {tag}")
    await c.close()

asyncio.run(main())
