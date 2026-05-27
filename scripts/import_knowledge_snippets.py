from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.db.session import AsyncSessionLocal
from app.services.knowledge_base import KnowledgeBaseService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import brain knowledge snippets from JSONL.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--embed", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        service = KnowledgeBaseService(session)
        created = await service.import_jsonl(args.path, source=args.source)
        embedded = await service.embed_missing() if args.embed else 0
    print(f"knowledge imported: created={created} embedded={embedded}")


if __name__ == "__main__":
    asyncio.run(main())
