from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import AsyncSessionLocal
from app.models.brain_v2 import LeadBrainState, LeadProfileSlot
from app.models.dialog import Dialog
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from scripts.run_controlled_funnel import CONTROL_KEY


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print a compact controlled funnel status report.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--limit", type=int, default=8)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    normalized = args.username.strip().lower().lstrip("@")
    async with AsyncSessionLocal() as session:
        dialog = (
            await session.execute(
                select(Dialog)
                .where(func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized)
                .order_by(Dialog.updated_at.desc(), Dialog.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if dialog is None:
            print(f"report_time={datetime.now(UTC).isoformat()} username={args.username} dialog=missing")
            return

        lead = (
            await session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        ).scalar_one_or_none()
        state = (
            await session.execute(
                select(LeadBrainState)
                .where(LeadBrainState.dialog_id == dialog.id)
                .order_by(LeadBrainState.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        phase = (state.metadata_json or {}).get(CONTROL_KEY) if state and state.metadata_json else {}
        slots = []
        if lead is not None:
            slots = (
                await session.execute(
                    select(LeadProfileSlot)
                    .where(LeadProfileSlot.lead_id == lead.id)
                    .order_by(LeadProfileSlot.slot_key.asc())
                )
            ).scalars().all()

        print(f"report_time={datetime.now(UTC).isoformat()}")
        print(f"dialog={dialog.id} username={dialog.telegram_username} updated_at={dialog.updated_at}")
        print(
            "lead="
            f"{lead.id if lead else '-'} qualification={lead.qualification_status if lead else '-'} "
            f"funnel_state={lead.funnel_state if lead else '-'} lost_reason={lead.lost_reason if lead else '-'}"
        )
        print(f"brain_stage={state.stage if state else '-'} brain_status={state.status if state else '-'}")
        print(
            "controlled_phase="
            f"{phase.get('phase', '-')} voice_pack={phase.get('voice_pack', '-')} "
            f"voice_index={phase.get('voice_index', '-')} last_voice_job_id={phase.get('last_voice_job_id', '-')}"
        )
        print(f"last_processed_inbound={phase.get('last_processed_inbound_message_id', '-')}")
        if slots:
            print(
                "slots="
                + json.dumps(
                    {
                        slot.slot_key: slot.slot_value_json
                        if slot.slot_value_json is not None
                        else slot.slot_value
                        for slot in slots
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )

        print("messages:")
        messages = (
            await session.execute(
                select(Message)
                .where(Message.dialog_id == dialog.id)
                .order_by(Message.created_at.desc())
                .limit(args.limit)
            )
        ).scalars().all()
        for message in messages:
            body = " ".join((message.body or "").split())[:140]
            print(f"  {message.created_at} {message.direction:<8} {message.status:<9} {message.id} {body}")

        print("outbound_jobs:")
        jobs = (
            await session.execute(
                select(OutboundJob)
                .where(OutboundJob.dialog_id == dialog.id)
                .order_by(OutboundJob.created_at.desc())
                .limit(args.limit)
            )
        ).scalars().all()
        for job in jobs:
            topic = (job.media_metadata or {}).get("controlled_topic") or (job.media_metadata or {}).get("topic") or "-"
            print(
                f"  {job.created_at} {job.status:<9} {job.job_type:<5} topic={topic} "
                f"job={job.id} msg={job.message_id} scheduled={job.scheduled_at} sent={job.sent_at}"
            )


if __name__ == "__main__":
    asyncio.run(main())
