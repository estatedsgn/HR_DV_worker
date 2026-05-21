import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.inbound_queue_worker import InboundQueueWorker


async def run_once(limit: int) -> None:
    async with AsyncSessionLocal() as session:
        worker = InboundQueueWorker(session)
        result = await worker.process_queued_batch(limit=limit)
    print(
        f"processed batch: total={result.total} processed={result.processed} "
        f"failed={result.failed} retry={result.retry} dead_letter={result.dead_letter}"
    )


async def main(limit: int, *, loop: bool, interval_seconds: int) -> None:
    while True:
        await run_once(limit)
        if not loop:
            return
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=10)
    args = parser.parse_args()
    asyncio.run(main(args.limit, loop=args.loop, interval_seconds=args.interval_seconds))
