import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)

    print("=== tables ===")
    tabs = [r['table_name'] for r in await c.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")]
    print(" ", tabs)
    inbound_tab = next((t for t in tabs if 'inbound' in t), None)
    print(f"\n=== {inbound_tab} by status ===")
    if inbound_tab:
        for r in await c.fetch(f"SELECT status, count(*) n FROM {inbound_tab} GROUP BY status ORDER BY status"):
            print(f"  {r['status']:12} x{r['n']}")

    print("\n=== accounts ===")
    for r in await c.fetch("SELECT telegram_username, health_status, next_available_at, flood_wait_until, send_interval_seconds FROM accounts"):
        print(f"  {dict(r)}")

    print("\n=== outbound_jobs global by status ===")
    for r in await c.fetch("SELECT status, count(*) n FROM outbound_jobs GROUP BY status ORDER BY status"):
        print(f"  {r['status']:12} x{r['n']}")

    print("\n=== dialogs count ===")
    print("  total dialogs:", await c.fetchval("SELECT count(*) FROM dialogs"))
    await c.close()

asyncio.run(main())
