from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.repositories.lead import LeadRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manually update mini-CRM lead status fields.")
    parser.add_argument("--dialog-id", required=True)
    parser.add_argument("--funnel-state")
    parser.add_argument("--qualification-status")
    parser.add_argument("--interest-status")
    parser.add_argument("--assigned-to")
    parser.add_argument("--next-step")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        lead = await LeadRepository(session).get_by_dialog(args.dialog_id)
        if lead is None:
            print("lead not found")
            return
        updates = {
            "funnel_state": args.funnel_state,
            "qualification_status": args.qualification_status,
            "interest_status": args.interest_status,
            "assigned_to": args.assigned_to,
            "next_step": args.next_step,
        }
        for field, value in updates.items():
            if value is not None:
                setattr(lead, field, value)
        await session.commit()
    print(f"lead updated: dialog_id={args.dialog_id}")


if __name__ == "__main__":
    asyncio.run(main())
