import asyncio
from app.db.session import AsyncSessionLocal
from sqlalchemy import text


async def main():
    async with AsyncSessionLocal() as s:
        cols = (
            await s.execute(
                text(
                    "select column_name from information_schema.columns "
                    "where table_name='accounts' order by ordinal_position"
                )
            )
        ).scalars().all()
        print("COLUMNS:", cols)
        print("---")
        rows = (await s.execute(text("select * from accounts order by created_at"))).mappings().all()
        for r in rows:
            d = dict(r)
            print({k: (str(v)[:14] if v is not None else None) for k, v in d.items()})


asyncio.run(main())
