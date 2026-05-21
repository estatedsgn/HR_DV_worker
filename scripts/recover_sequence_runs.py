from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.campaign_sequence import CampaignSequenceService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover stale dialog sequence runs.")
    parser.add_argument("--older-than-seconds", type=int, default=900)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        result = await CampaignSequenceService(session).recover_stale_runs(
            older_than_seconds=args.older_than_seconds
        )
    print(f"sequence recovery: recovered={result['recovered']} failed={result['failed']}")


if __name__ == "__main__":
    asyncio.run(main())
