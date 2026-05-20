import argparse
import asyncio

from app.db.session import SessionLocal
from app.services.inbound_queue_worker import InboundQueueWorker


async def main(limit: int) -> None:
    async with SessionLocal() as session:
        worker = InboundQueueWorker(session)
        result = await worker.process_queued_batch(limit=limit)
    print(
        f"processed batch: total={result.total} processed={result.processed} "
        f"failed={result.failed} retry={result.retry}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    asyncio.run(main(args.limit))
