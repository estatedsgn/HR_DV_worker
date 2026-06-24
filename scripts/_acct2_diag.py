import asyncio, os, sys
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

ACC2 = "fa819fd6-293e-4ffb-a00c-97df57873896"

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    now = datetime.now(timezone.utc)
    print("now(UTC):", str(now)[:19])
    print("\n=== accounts ===")
    for r in await c.fetch("SELECT id, crmchat_account_id, health_status, next_available_at, flood_wait_until, send_interval_seconds FROM accounts ORDER BY id"):
        na = r['next_available_at']; fw = r['flood_wait_until']
        na_in = f"{(na-now).total_seconds():.0f}s" if na else "-"
        fw_in = f"{(fw-now).total_seconds():.0f}s" if fw else "-"
        print(f"  {str(r['id'])[:8]} crm={r['crmchat_account_id']} health={r['health_status']} next_avail_in={na_in} flood_in={fw_in} interval={r['send_interval_seconds']}")
    print("\n=== outbound_jobs for ACC2 by status ===")
    for r in await c.fetch("SELECT status, count(*) n FROM outbound_jobs WHERE account_id=$1 GROUP BY status ORDER BY n DESC", ACC2):
        print(f"  {str(r['status']):12} x{r['n']}")
    print("\n=== ACC2 jobs not yet sent (queued/retry/processing/scheduled) — next 15 by scheduled ===")
    rows = await c.fetch("""
        SELECT j.status, j.scheduled_at, j.next_attempt_at, j.attempt_count, d.telegram_username,
               left(coalesce(j.error_message,''),46) err
        FROM outbound_jobs j LEFT JOIN dialogs d ON d.id=j.dialog_id
        WHERE j.account_id=$1 AND j.status NOT IN ('sent','cancelled','blocked','dead_letter','failed')
        ORDER BY coalesce(j.next_attempt_at, j.scheduled_at) LIMIT 15
    """, ACC2)
    if not rows:
        print("  (none pending)")
    for r in rows:
        sched = r['next_attempt_at'] or r['scheduled_at']
        due = f"{(sched-now).total_seconds():.0f}s" if sched else "?"
        print(f"  {str(r['status']):10} {str(r['telegram_username']):18} att={r['attempt_count']} due_in={due} err={r['err']!r}")
    await c.close()

asyncio.run(main())
