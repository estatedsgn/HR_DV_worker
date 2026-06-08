"""Одноразовая правка: снять 5-минутную стену пейсинга с существующих аккаунтов.

Старый дефолт send_interval_seconds=300/jitter=60 создавал минуты задержки между
исходящими (next_available_at гейтил каждый следующий job). Приводим живые строки
к новому маленькому интервалу и освобождаем заблокированные аккаунты.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select, update

from app.db.session import AsyncSessionLocal
from app.models.account import Account

NEW_INTERVAL = 3
NEW_JITTER = 3


async def main() -> None:
    async with AsyncSessionLocal() as session:
        before = (await session.execute(select(Account.id, Account.send_interval_seconds, Account.send_jitter_seconds))).all()
        await session.execute(
            update(Account).values(
                send_interval_seconds=NEW_INTERVAL,
                send_jitter_seconds=NEW_JITTER,
                next_available_at=None,
            )
        )
        await session.commit()
        print(f"updated {len(before)} accounts -> interval={NEW_INTERVAL}s jitter={NEW_JITTER}s, next_available_at cleared")
        for row in before:
            print(f"  {row[0]}: was interval={row[1]} jitter={row[2]}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
