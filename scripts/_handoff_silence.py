"""Ad-hoc: list handoff/paused dialogs and cancel their queued outbound so the
bot can't fire a leftover message after a human took over. Read-only unless --apply."""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import asyncpg

from app.core.config import get_settings

APPLY = "--apply" in sys.argv

SEL = """
SELECT d.telegram_username, r.stage, r.status, d.id AS dialog_id
FROM lead_funnel_runtime r
JOIN leads l ON l.id = r.lead_id
JOIN dialogs d ON d.id = l.dialog_id
WHERE r.status = 'handoff' OR r.stage = 'human_handoff'
   OR (r.metadata_json ->> 'bot_paused') = 'true'
"""

CANCEL = """
UPDATE outbound_jobs SET status='cancelled', next_attempt_at=NULL,
       lease_owner=NULL, lease_expires_at=NULL, error_message='cancelled: human handoff'
WHERE status IN ('queued','retry','processing')
  AND dialog_id IN (
    SELECT l.dialog_id FROM lead_funnel_runtime r JOIN leads l ON l.id = r.lead_id
    WHERE r.status='handoff' OR r.stage='human_handoff'
       OR (r.metadata_json ->> 'bot_paused')='true')
"""


async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    rows = await c.fetch(SEL)
    print(f"HANDOFF/PAUSED DIALOGS ({len(rows)}):")
    for r in rows:
        print(f"  {r['telegram_username']}  stage={r['stage']} status={r['status']}")
    pending = await c.fetchval(
        "SELECT count(*) FROM outbound_jobs WHERE status IN ('queued','retry','processing') "
        "AND dialog_id IN (SELECT l.dialog_id FROM lead_funnel_runtime r JOIN leads l ON l.id=r.lead_id "
        "WHERE r.status='handoff' OR r.stage='human_handoff' OR (r.metadata_json->>'bot_paused')='true')"
    )
    print(f"PENDING OUTBOUND for those dialogs: {pending}")
    if APPLY:
        res = await c.execute(CANCEL)
        print(f"CANCELLED: {res}")
    else:
        print("(dry run — pass --apply to cancel)")
    await c.close()


asyncio.run(main())
