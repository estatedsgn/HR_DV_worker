"""Prove that a Дайвинчик match is persisted to the database.

Runs the *real* lead-capture path (LeadIntakeService.enqueue_lead, the same code
the auto-swiper calls on a mutual match), reads the rows back from every table
it touches, prints them, then deletes the synthetic test data so nothing is left
behind. The Telegram alert is disabled so this does not ping the operator.

    .venv\\Scripts\\python.exe scripts\\verify_daivinchik_lead.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.lead import Lead
from app.models.lead_intake_event import LeadIntakeEvent
from app.models.outbound_job import OutboundJob
from app.services.lead_intake import LeadIntakeService
from app.services.lead_notifier import LeadNotifier


async def main() -> int:
    ts = int(time.time())
    external_id = f"id:VERIFY{ts}"
    username = f"@dv_verify_{ts}"
    crm_dialog_id = f"intake:daivinchik:{external_id}"

    # Notifier with all alerts disabled -> no Telegram ping during the test.
    notifier = LeadNotifier()
    notifier.settings = SimpleNamespace(
        lead_alerts_enabled=False, control_bot_token=None, control_admin_chat_id=None
    )

    ok = False
    try:
        async with AsyncSessionLocal() as session:
            result = await LeadIntakeService(session, notifier=notifier).enqueue_lead(
                source="daivinchik",
                external_lead_id=external_id,
                telegram_username=username,
                payload={
                    "display_name": "VERIFY",
                    "profile_text": "verification match",
                    "telegram_user_id": str(ts),
                    "link": f"tg://user?id={ts}",
                    "match_message_id": 0,
                },
            )
        print(f"enqueue_lead OK (idempotent={result.idempotent})")

        async with AsyncSessionLocal() as session:
            events = (
                await session.execute(
                    select(LeadIntakeEvent).where(LeadIntakeEvent.external_lead_id == external_id)
                )
            ).scalars().all()
            dialogs = (
                await session.execute(
                    select(Dialog).where(Dialog.crmchat_dialog_id == crm_dialog_id)
                )
            ).scalars().all()
            dialog_ids = [d.id for d in dialogs]
            leads = (
                (await session.execute(select(Lead).where(Lead.dialog_id.in_(dialog_ids)))).scalars().all()
                if dialog_ids
                else []
            )
            runs = (
                (await session.execute(select(DialogSequenceRun).where(DialogSequenceRun.dialog_id.in_(dialog_ids)))).scalars().all()
                if dialog_ids
                else []
            )

            print("\n--- rows written ---")
            print("lead_intake_events:", [(e.source, e.external_lead_id, e.telegram_username, e.status) for e in events])
            print("dialogs:           ", [(d.crmchat_dialog_id, d.telegram_username, d.status) for d in dialogs])
            print("leads:             ", [(l.qualification_status, l.funnel_state) for l in leads])
            print("dialog_sequence_run:", [(r.status, r.current_step_position) for r in runs])
            ok = bool(events and dialogs and leads)
            print("\nRESULT:", "PASS — match persisted to DB" if ok else "FAIL — rows missing")
    finally:
        async with AsyncSessionLocal() as session:
            dialogs = (
                await session.execute(select(Dialog).where(Dialog.crmchat_dialog_id == crm_dialog_id))
            ).scalars().all()
            dialog_ids = [d.id for d in dialogs]
            if dialog_ids:
                await session.execute(delete(OutboundJob).where(OutboundJob.dialog_id.in_(dialog_ids)))
                await session.execute(delete(DialogSequenceRun).where(DialogSequenceRun.dialog_id.in_(dialog_ids)))
            await session.execute(delete(LeadIntakeEvent).where(LeadIntakeEvent.external_lead_id == external_id))
            for dialog in dialogs:
                await session.delete(dialog)  # cascades lead/messages/logs
            await session.commit()
        print("cleanup done (test rows removed)")

    return 0 if ok else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
