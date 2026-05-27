from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.lead import Lead
from app.models.outbound_job import OutboundJob


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive local test conversation state for a username."
    )
    parser.add_argument("--username", required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    username = normalize_username(args.username)
    now = datetime.now(UTC)
    archived_leads = cancelled_jobs = closed_runs = 0

    async with AsyncSessionLocal() as session:
        dialogs = (
            await session.execute(
                select(Dialog).where(Dialog.telegram_username.ilike(username.lstrip("@")))
            )
        ).scalars().all()
        if not dialogs:
            dialogs = (
                await session.execute(
                    select(Dialog).where(Dialog.telegram_username.ilike(username))
                )
            ).scalars().all()

        for dialog in dialogs:
            leads = (
                await session.execute(select(Lead).where(Lead.dialog_id == dialog.id))
            ).scalars().all()
            for lead in leads:
                if lead.funnel_state not in {
                    "CONVERTED",
                    "LOST",
                    "DO_NOT_CONTACT",
                    "HUMAN_HANDOFF",
                }:
                    lead.funnel_state = "LOST"
                    lead.qualification_status = "not_qualified"
                    lead.next_step = None
                    lead.lost_reason = "local test reset"
                    archived_leads += 1

            runs = (
                await session.execute(
                    select(DialogSequenceRun).where(DialogSequenceRun.dialog_id == dialog.id)
                )
            ).scalars().all()
            for run in runs:
                if run.status in {"active", "waiting_outbound", "awaiting_reply", "awaiting_llm"}:
                    run.status = "completed"
                    run.completed_at = now
                    run.error_message = "local test reset"
                    closed_runs += 1

            jobs = (
                await session.execute(
                    select(OutboundJob).where(OutboundJob.dialog_id == dialog.id)
                )
            ).scalars().all()
            for job in jobs:
                if job.status in {"queued", "retry", "processing"}:
                    job.status = "cancelled"
                    job.next_attempt_at = None
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.error_message = "local test reset"
                    cancelled_jobs += 1

        await session.commit()

    print(
        "local test reset: "
        f"username={username} dialogs={len(dialogs)} "
        f"archived_leads={archived_leads} closed_runs={closed_runs} "
        f"cancelled_jobs={cancelled_jobs}"
    )


def normalize_username(value: str) -> str:
    username = value.strip()
    if not username:
        raise ValueError("--username is required")
    return username if username.startswith("@") else f"@{username}"


if __name__ == "__main__":
    asyncio.run(main())
