from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.models.campaign import Campaign, CampaignStep
from app.repositories.campaign import CampaignRepository
from sqlalchemy import select


BRAIN_CAMPAIGN_NAME = "brain-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the Brain V1 test campaign.")
    parser.add_argument("--name", default=BRAIN_CAMPAIGN_NAME)
    parser.add_argument("--make-default", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        repository = CampaignRepository(session)
        campaign = await repository.get_by_name_with_steps(args.name)
        if campaign is None:
            campaign = Campaign(
                name=args.name,
                description="Brain V1 funnel: opener, interest gate, fixed info, repeating LLM qualification.",
                status="active",
                is_default=args.make_default,
                metadata_json={"version": 1, "kind": "brain"},
            )
            session.add(campaign)
            await session.flush()
        else:
            campaign.status = "active"
            campaign.is_default = args.make_default or campaign.is_default
            campaign.description = "Brain V1 funnel: opener, interest gate, fixed info, repeating LLM qualification."
            campaign.metadata_json = {"version": 1, "kind": "brain"}

        steps_result = await session.execute(
            select(CampaignStep).where(CampaignStep.campaign_id == campaign.id)
        )
        existing_by_position = {
            step.position: step for step in steps_result.scalars().all()
        }
        for spec in brain_steps(campaign.id):
            step = existing_by_position.get(spec["position"])
            if step is None:
                session.add(CampaignStep(campaign_id=campaign.id, **spec))
            else:
                step.step_type = spec["step_type"]
                step.message_text = spec["message_text"]
                step.delay_seconds = spec["delay_seconds"]
                step.wait_for_reply = spec["wait_for_reply"]
                step.metadata_json = spec["metadata_json"]
        await session.commit()
    print(f"brain campaign seeded: name={campaign.name} id={campaign.id}")


def brain_steps(campaign_id) -> list[dict]:
    return [
        {
            "position": 1,
            "step_type": "fixed_message",
            "message_text": (
                "Привет, классно выглядишь. Подумала, может тебе будет интересно "
                "вместе поработать. Хочешь расскажу, где я работаю?"
            ),
            "delay_seconds": 0,
            "wait_for_reply": True,
            "metadata_json": {"state_after": "WAITING_FIRST_REPLY"},
        },
        {
            "position": 2,
            "step_type": "interest_gate",
            "message_text": None,
            "delay_seconds": 0,
            "wait_for_reply": False,
            "metadata_json": {"state_after": "INTEREST_CLASSIFICATION"},
        },
        {
            "position": 3,
            "step_type": "fixed_message",
            "message_text": (
                "Смотри, если коротко: у нас формат удаленный, общение с людьми, "
                "без офиса и жесткого графика. Я сама сначала просто уточняю, "
                "кому это может быть интересно."
            ),
            "delay_seconds": 0,
            "wait_for_reply": False,
            "metadata_json": {"state_after": "INFO_SENT"},
        },
        {
            "position": 4,
            "step_type": "fixed_message",
            "message_text": (
                "По условиям лучше расскажу аккуратно по шагам, чтобы не грузить. "
                "Если тебе в целом ок такой формат, я задам пару вопросов и передам тебя дальше."
            ),
            "delay_seconds": 0,
            "wait_for_reply": True,
            "metadata_json": {"state_after": "WAITING_AFTER_INFO"},
        },
        {
            "position": 5,
            "step_type": "brain_turn",
            "message_text": None,
            "delay_seconds": 0,
            "wait_for_reply": True,
            "metadata_json": {"repeat": True, "state_after": "QUALIFICATION_IN_PROGRESS"},
        },
    ]


if __name__ == "__main__":
    asyncio.run(main())
