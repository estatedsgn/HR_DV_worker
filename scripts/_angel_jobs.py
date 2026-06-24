import asyncio, os, sys
from datetime import datetime, timezone
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    rows = await c.fetch("""
        SELECT j.status, j.job_type, j.attempt_count, j.scheduled_at, j.sent_at, j.last_error_type,
               left(coalesce(j.error_message,''),80) err
        FROM outbound_jobs j JOIN dialogs d ON d.id=j.dialog_id
        WHERE replace(lower(d.telegram_username),'@','')='angel_12896'
        ORDER BY j.created_at
    """)
    now = datetime.now(timezone.utc)
    for r in rows:
        sched = r['scheduled_at']
        due = f"due_in={(sched-now).total_seconds():.0f}s" if sched else "due=?"
        print(f"  {r['status']:11} att={r['attempt_count']} {due} errtype={r['last_error_type']} err={r['err']!r}")
    await c.close()

asyncio.run(main())
