import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import asyncpg

DIALOG_ID = 'b840bf4a-5f26-42c9-bdfb-cce4f19f4d30'

async def main():
    dsn = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
    conn = await asyncpg.connect(dsn)

    # messages columns
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='messages' ORDER BY ordinal_position"
    )
    print("messages columns:", [r['column_name'] for r in cols])

    # funnel_states columns
    fs_cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='funnel_states' ORDER BY ordinal_position"
    )
    print("funnel_states columns:", [r['column_name'] for r in fs_cols])

    cnt = await conn.fetchval("SELECT COUNT(*) FROM messages WHERE dialog_id=$1::uuid", DIALOG_ID)
    print(f"\nTotal messages in dialog: {cnt}")

    msgs = await conn.fetch(
        "SELECT * FROM messages WHERE dialog_id=$1::uuid ORDER BY created_at DESC LIMIT 10",
        DIALOG_ID
    )
    print(f"\nLast {len(msgs)} messages:")
    for m in reversed(msgs):
        d = dict(m)
        print(f"  [{d.get('created_at','')}] {d}")

    # funnel state
    fs = await conn.fetchrow("SELECT * FROM funnel_states WHERE dialog_id=$1::uuid", DIALOG_ID)
    if fs:
        print("\nFunnel state:", dict(fs))
    else:
        print("\nNo funnel_state found for this dialog")

    await conn.close()

asyncio.run(main())
