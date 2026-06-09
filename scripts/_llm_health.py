import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from app.core.config import get_settings
import asyncpg

async def main():
    s = get_settings()
    keys = getattr(s, "llm_api_keys", None)
    print("LLM key pool size:", len(keys) if keys else "n/a")
    dsn = s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    cols = [r['column_name'] for r in await c.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='llm_calls' ORDER BY ordinal_position")]
    print("llm_calls cols:", cols)
    print("\n=== llm_calls last 15 ===")
    rows = await c.fetch("SELECT * FROM llm_calls ORDER BY created_at DESC LIMIT 15")
    for r in rows:
        d = dict(r)
        keep = {k: str(v)[:40] for k,v in d.items() if k in ('model','status','error','error_message','latency_ms','created_at','success')}
        print("  ", keep)
    await c.close()

asyncio.run(main())
