from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.prompt_versions import PromptVersionService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed active brain prompt versions.")
    parser.add_argument("--name", default="brain_main")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--file-name", default="brain_main_v1.md")
    parser.add_argument("--no-activate", action="store_true")
    parser.add_argument("--changelog", default="Initial brain prompt.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        result = await PromptVersionService(session).seed_from_file(
            name=args.name,
            version=args.version,
            file_name=args.file_name,
            activate=not args.no_activate,
            changelog=args.changelog,
        )
    print(
        "prompt seeded: "
        f"name={result.prompt.name} version={result.prompt.version} "
        f"status={result.prompt.status} created={result.created} updated={result.updated}"
    )


if __name__ == "__main__":
    asyncio.run(main())
