"""Нейтрализовать мусорный backlog входящих, поднятый случайным poll-all.

Помечает queued inbound_events НЕ из целевых диалогов как 'processed', чтобы
inbound-воркер не гонял их через граф (не шлёт ничего наружу — наоборот, гасит
обработку чужих демо-диалогов). Целевые диалоги защищены явным исключением.
"""
import asyncio, os, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core.config import get_settings
import asyncpg

KEEP = (
    "cef468a5-1834-4ad1-9479-6e9e4a2c4162",  # @hunt_pavluck
    "bc144b7e-cd79-4724-bef5-4e3a87f40a22",  # @siloxxx949
)

async def main():
    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    c = await asyncpg.connect(dsn)
    before = await c.fetchval("SELECT count(*) FROM inbound_events WHERE status='queued'")
    keep_q = await c.fetchval(
        "SELECT count(*) FROM inbound_events WHERE status='queued' AND dialog_id = ANY($1::uuid[])",
        list(KEEP))
    res = await c.execute(
        "UPDATE inbound_events SET status='processed', processed_at=now(), "
        "error_message='drained: junk backlog from accidental poll-all', updated_at=now() "
        "WHERE status='queued' AND NOT (dialog_id = ANY($1::uuid[]))",
        list(KEEP))
    after = await c.fetchval("SELECT count(*) FROM inbound_events WHERE status='queued'")
    print(f"queued before={before}, target-kept={keep_q}, update={res}, queued after={after}")
    await c.close()

asyncio.run(main())
