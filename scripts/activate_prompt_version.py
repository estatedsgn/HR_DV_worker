from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.prompt_versions import PromptVersionService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Activate an existing prompt version.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        prompt = await PromptVersionService(session).activate(
            name=args.name,
            version=args.version,
        )
    print(f"prompt activated: name={prompt.name} version={prompt.version}")


if __name__ == "__main__":
    asyncio.run(main())
