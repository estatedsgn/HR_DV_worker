from __future__ import annotations

import asyncio

from app.db.session import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStep
from app.repositories.campaign import CampaignRepository

CAMPAIGN_NAME = "soul-chat-test-v1"


async def main() -> None:
    async with AsyncSessionLocal() as session:
        existing = await CampaignRepository(session).get_by_name_with_steps(CAMPAIGN_NAME)
        if existing:
            print(f"soul chat campaign ready: id={existing.id} name={existing.name}")
            return
        campaign = Campaign(
            name=CAMPAIGN_NAME,
            description="Short test funnel: fixed opener, then immediate warm LLM conversation.",
            status="active",
            is_default=False,
            metadata_json={"kind": "llm_test", "version": 1},
        )
        session.add(campaign)
        await session.flush()
        session.add_all(
            [
                CampaignStep(
                    campaign_id=campaign.id,
                    position=1,
                    step_type="fixed_message",
                    message_text=(
                        "Привет. Я на связи, можно просто спокойно поговорить. "
                        "Расскажи, что сейчас у тебя на душе или что тебе интересно?"
                    ),
                    delay_seconds=0,
                    wait_for_reply=True,
                    metadata_json={},
                ),
                CampaignStep(
                    campaign_id=campaign.id,
                    position=2,
                    step_type="llm_decision",
                    delay_seconds=0,
                    wait_for_reply=False,
                    metadata_json={"prompt": "lead_decision"},
                ),
            ]
        )
        await session.commit()
        print(f"soul chat campaign ready: id={campaign.id} name={campaign.name}")


if __name__ == "__main__":
    asyncio.run(main())
