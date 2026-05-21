from __future__ import annotations

import asyncio

from app.db.session import AsyncSessionLocal
from app.services.campaign_defaults import DefaultCampaignService


async def main() -> None:
    async with AsyncSessionLocal() as session:
        campaign = await DefaultCampaignService(session).ensure_default_campaign()
        await session.commit()
    print(f"default campaign ready: id={campaign.id} name={campaign.name}")


if __name__ == "__main__":
    asyncio.run(main())
