from __future__ import annotations

import argparse
import asyncio
import json

from app.db.session import AsyncSessionLocal
from app.services.lead_intake import LeadIntakeService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enqueue a lead into the HR DV worker.")
    parser.add_argument("--source", default="internal")
    parser.add_argument("--external-lead-id", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--campaign-id")
    parser.add_argument("--payload-json", default="{}")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    payload = json.loads(args.payload_json)
    if not isinstance(payload, dict):
        raise ValueError("--payload-json must decode to a JSON object")
    async with AsyncSessionLocal() as session:
        result = await LeadIntakeService(session).enqueue_lead(
            source=args.source,
            external_lead_id=args.external_lead_id,
            telegram_username=args.username,
            campaign_id=args.campaign_id,
            payload=payload,
        )
    print(
        "lead intake: "
        f"status={result.event.status} idempotent={result.idempotent} "
        f"event_id={result.event.id} dialog_id={result.event.dialog_id} "
        f"account_id={result.event.account_id}"
    )


if __name__ == "__main__":
    asyncio.run(main())
