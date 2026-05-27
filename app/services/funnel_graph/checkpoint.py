from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import Settings


@asynccontextmanager
async def postgres_checkpointer(settings: Settings, *, setup: bool = False) -> AsyncIterator[Any]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    conn_string = psycopg_conn_string(settings.database_url)
    async with AsyncPostgresSaver.from_conn_string(conn_string) as saver:
        if setup:
            await saver.setup()
        yield saver


async def setup_postgres_checkpointer(settings: Settings) -> None:
    async with postgres_checkpointer(settings, setup=True):
        return


@asynccontextmanager
async def configured_checkpointer(settings: Settings, explicit: Any | None = None) -> AsyncIterator[Any]:
    if explicit is not None:
        yield explicit
        return
    if not settings.langgraph_checkpoint_postgres_enabled:
        yield False
        return
    async with postgres_checkpointer(settings, setup=False) as saver:
        yield saver


def psycopg_conn_string(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
