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
    did = DIALOGS.get(sys.argv[1]) if len(sys.argv) > 1 else DIALOGS["@hunt_pavluck"]

    print("=== MESSAGE TIMELINE (last 25) ===")
    rows = await c.fetch(
        "SELECT direction, status, "
        "coalesce(sent_at, created_at) AS ts, left(coalesce(body,''),40) AS t "
        "FROM messages WHERE dialog_id=$1::uuid "
        "ORDER BY coalesce(sent_at, created_at) DESC LIMIT 25", did)
    rows = list(reversed(rows))
    prev = None
    for r in rows:
        gap = ""
        if prev is not None and r["ts"] is not None and prev is not None:
            d = (r["ts"] - prev).total_seconds()
            gap = f"  (+{d:.0f}s)"
        prev = r["ts"]
        print(f"  {str(r['ts'])[:19]} {r['direction']:8} {r['status']:9}{gap}  {r['t']!r}")

    print("\n=== OUTBOUND JOB LIFECYCLE (last 15) ===")
    jobs = await c.fetch(
        "SELECT job_type, status, created_at, scheduled_at, sent_at, attempt_count, "
        "left(coalesce(error_message,''),50) AS err "
        "FROM outbound_jobs WHERE dialog_id=$1::uuid "
        "ORDER BY created_at DESC LIMIT 15", did)
    for j in reversed(jobs):
        sched_lag = (j["scheduled_at"] - j["created_at"]).total_seconds() if j["scheduled_at"] and j["created_at"] else None
        sent_lag = (j["sent_at"] - j["scheduled_at"]).total_seconds() if j["sent_at"] and j["scheduled_at"] else None
        print(f"  {j['job_type']:5} {j['status']:9} att={j['attempt_count']} "
              f"created={str(j['created_at'])[:19]} sched+{sched_lag}s sent_after_sched={sent_lag}s  {j['err']!r}")
    await c.close()

asyncio.run(main())
