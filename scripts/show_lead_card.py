from __future__ import annotations

import argparse
import asyncio
import json

from app.db.session import AsyncSessionLocal
from app.repositories.lead import LeadRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show a mini-CRM lead card by dialog id.")
    parser.add_argument("--dialog-id", required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        lead = await LeadRepository(session).get_by_dialog_with_facts(args.dialog_id)
        if lead is None:
            print("lead not found")
            return
        payload = {
            "id": str(lead.id),
            "dialog_id": str(lead.dialog_id),
            "qualification_status": lead.qualification_status,
            "funnel_state": lead.funnel_state,
            "interest_status": lead.interest_status,
            "score": lead.score,
            "summary": lead.summary,
            "next_step": lead.next_step,
            "assigned_to": lead.assigned_to,
            "lost_reason": lead.lost_reason,
            "do_not_contact_reason": lead.do_not_contact_reason,
            "facts": {
                fact.fact_key: fact.fact_value_json if fact.fact_value_json is not None else fact.fact_value
                for fact in lead.facts
            },
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
