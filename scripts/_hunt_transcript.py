import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

DIALOGS = {
    "@hunt_pavluck": "cef468a5-1834-4ad1-9479-6e9e4a2c4162",
    "@siloxxx949": "bc144b7e-cd79-4724-bef5-4e3a87f40a22",
}
# можно передать конкретный dialog_id аргументом, иначе печатает все
ARG = sys.argv[1] if len(sys.argv) > 1 else None

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    cols = [r["column_name"] for r in await c.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1", "messages")]
    textcol = next((x for x in ("content", "text", "body", "message_text") if x in cols), None)
    targets = {k: v for k, v in DIALOGS.items() if ARG in (None, v, k)}
    for name, did in targets.items():
        print(f"\n===== {name} ({did}) =====")
        rows = await c.fetch(
            f"SELECT direction, {textcol} AS t, created_at FROM messages "
            "WHERE dialog_id=$1::uuid ORDER BY created_at", did)
        for r in rows:
            who = "БОТ   " if r["direction"] == "outbound" else "ЛИД   "
            print(f'[{r["created_at"]:%H:%M}] {who}: {r["t"]}')
        if not rows:
            print("(пока нет сообщений)")
    await c.close()

asyncio.run(main())
