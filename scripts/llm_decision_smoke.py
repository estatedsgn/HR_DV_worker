from __future__ import annotations

import argparse
import asyncio

from app.services.llm_adapter import LLMAPIError, LLMAdapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one LLM decision smoke without sending messages.")
    parser.add_argument("--text", default="Здравствуйте, мне интересно, расскажите подробнее.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with LLMAdapter() as adapter:
        decision = await adapter.decide_next_action(
            dialog_messages=[{"direction": "inbound", "sender_type": "lead", "body": args.text}],
            lead_context={"dialog_status": "open", "lead_status": "new"},
        )
    print(
        "llm decision: "
        f"decision={decision.decision} lead_status={decision.lead_status} "
        f"confidence={decision.confidence} has_reply={bool(decision.reply_text)}"
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LLMAPIError as exc:
        print(f"LLM smoke failed: {exc}")
        raise SystemExit(1) from exc
