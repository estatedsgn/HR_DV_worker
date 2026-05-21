from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.crmchat_connector import CRMChatAPIError, CRMChatConnector
from app.services.outbound_queue_worker import OutboundQueueWorker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process queued outbound Telegram sends.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=10)
    parser.add_argument(
        "--allow-real-send",
        action="store_true",
        help="Allow real CRMChat sends. Still restricted by OUTBOUND_ALLOWED_USERNAMES.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.allow_real_send:
        print("outbound worker skipped: pass --allow-real-send to mutate/send queued jobs")
        return
    async with CRMChatConnector() as connector:
        while True:
            async with AsyncSessionLocal() as session:
                worker = OutboundQueueWorker(
                    session,
                    connector=connector,
                    allow_real_send=args.allow_real_send,
                )
                result = await worker.process_queued_batch(limit=args.limit)
            print(
                "outbound batch: "
                f"total={result.total} sent={result.sent} blocked={result.blocked} "
                f"retry={result.retry} failed={result.failed} dead_letter={result.dead_letter} "
                f"rescheduled={result.rescheduled}"
            )
            if not args.loop:
                return
            await asyncio.sleep(args.interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CRMChatAPIError as exc:
        print(f"CRMchat API error: {exc}")
        raise SystemExit(1) from exc
