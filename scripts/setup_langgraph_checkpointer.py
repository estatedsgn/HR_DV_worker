from __future__ import annotations

import asyncio
import sys

from app.core.config import get_settings
from app.services.funnel_graph.checkpoint import setup_postgres_checkpointer


async def main() -> None:
    await setup_postgres_checkpointer(get_settings())
    print("langgraph postgres checkpointer is ready")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
