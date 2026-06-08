import asyncio, os, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

DIALOG_ID = 'cef468a5-1834-4ad1-9479-6e9e4a2c4162'

async def main():
    dsn = get_settings().database_url.replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(dsn)

    print("=== outbound_jobs ===")
    jobs = await conn.fetch(
        "SELECT * FROM outbound_jobs WHERE dialog_id=$1::uuid ORDER BY created_at",
        DIALOG_ID,
    )
    for j in jobs:
        print({k: v for k, v in dict(j).items() if k not in ('payload_json',)})
    if not jobs:
        print("(нет джобов)")

    print("\n=== accounts schedule ===")
    accs = await conn.fetch(
        "SELECT id, telegram_username, health_status, next_available_at, send_interval_seconds FROM accounts"
    )
    for a in accs:
        print(dict(a))

    print("\n=== messages ===")
    msgs = await conn.fetch(
        "SELECT * FROM messages WHERE dialog_id=$1::uuid ORDER BY created_at",
        DIALOG_ID,
    )
    for m in msgs:
        d = dict(m)
        print({k: d.get(k) for k in ('direction', 'status', 'content', 'created_at') if k in d} or d)
    if not msgs:
        print("(нет сообщений)")

    await conn.close()

asyncio.run(main())
