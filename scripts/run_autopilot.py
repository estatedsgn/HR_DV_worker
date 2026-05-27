from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime

from sqlalchemy import update

from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.services.account_sync import AccountSyncService
from app.services.campaign_sequence import CampaignSequenceService
from app.services.crmchat_connector import CRMChatAPIError, CRMChatConnector
from app.services.inbound_queue_worker import InboundQueueWorker
from app.services.outbound_queue_worker import OutboundQueueWorker
from app.services.telegram_polling import TelegramPollingService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the full HR DV worker loop: poll, inbound, LLM recovery, outbound."
    )
    parser.add_argument("--all-accounts", action="store_true", help="Poll every active Telegram account.")
    parser.add_argument("--only-username", help="Only sync one username, for example @iamnekiy.")
    parser.add_argument(
        "--mark-read",
        action="store_true",
        help="Deprecated alias kept for old commands. Reads are now marked after sending the next reply.",
    )
    parser.add_argument(
        "--mark-read-on-poll",
        action="store_true",
        help="Immediately mark synced inbound messages as read during polling.",
    )
    parser.add_argument("--allow-real-send", action="store_true", help="Allow real sends; still restricted by allowlist.")
    parser.add_argument("--poll-interval-seconds", type=int, default=3)
    parser.add_argument("--inbound-limit", type=int, default=200)
    parser.add_argument("--outbound-limit", type=int, default=50)
    parser.add_argument("--recover-older-than-seconds", type=int, default=10)
    parser.add_argument("--sync-accounts-every", type=int, default=60)
    parser.add_argument("--test-fast-pacing-seconds", type=int, default=None)
    parser.add_argument("--typing-delay-seconds", type=float, default=None)
    parser.add_argument(
        "--min-inbound-before-handoff",
        type=int,
        default=None,
        help="Test mode: keep the brain talking until this many inbound messages are collected before handoff.",
    )
    parser.add_argument("--stop-after-cycles", type=int, default=None)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if args.min_inbound_before_handoff is not None:
        os.environ["BRAIN_MIN_INBOUND_BEFORE_HANDOFF"] = str(
            max(0, args.min_inbound_before_handoff)
        )
    if not args.allow_real_send:
        print("autopilot refused to start: pass --allow-real-send to send Telegram messages")
        raise SystemExit(2)

    cycle = 0
    async with CRMChatConnector() as connector:
        while True:
            cycle += 1
            started = datetime.now(UTC)
            try:
                async with AsyncSessionLocal() as session:
                    if cycle == 1 or cycle % max(1, args.sync_accounts_every) == 0:
                        sync = await AccountSyncService(session, connector=connector).sync_active_accounts()
                        print(
                            f"[{cycle}] account_sync total_remote={sync.total_remote} "
                            f"active_remote={sync.active_remote} created={sync.created} updated={sync.updated}"
                        )
                    if args.test_fast_pacing_seconds is not None:
                        await set_fast_pacing(session, args.test_fast_pacing_seconds)

                    polling = TelegramPollingService(
                        session,
                        connector=connector,
                        only_username=args.only_username,
                        mark_read=args.mark_read_on_poll,
                    )
                    poll_result = (
                        await polling.poll_all_active_accounts_once()
                        if args.all_accounts
                        else await polling.poll_once()
                    )
                    inbound_result = await InboundQueueWorker(
                        session, lease_owner="autopilot-inbound"
                    ).process_queued_batch(limit=args.inbound_limit)
                    recovery_result = await CampaignSequenceService(session).recover_stale_runs(
                        older_than_seconds=args.recover_older_than_seconds
                    )
                    outbound_result = await OutboundQueueWorker(
                        session,
                        connector=connector,
                        lease_owner="autopilot-outbound",
                        allow_real_send=True,
                        typing_delay_seconds=args.typing_delay_seconds,
                    ).process_queued_batch(limit=args.outbound_limit)

                elapsed = (datetime.now(UTC) - started).total_seconds()
                print(
                    f"[{cycle}] ok elapsed={elapsed:.1f}s "
                    f"poll={poll_result.status}/created:{poll_result.messages_created} "
                    f"inbound=p:{inbound_result.processed},f:{inbound_result.failed},r:{inbound_result.retry} "
                    f"recovery={recovery_result} "
                    f"outbound=s:{outbound_result.sent},res:{outbound_result.rescheduled},f:{outbound_result.failed}"
                )
            except CRMChatAPIError as exc:
                print(f"[{cycle}] crmchat_error={exc}")
            except Exception as exc:
                print(f"[{cycle}] autopilot_error={type(exc).__name__}: {exc}")

            if args.stop_after_cycles is not None and cycle >= args.stop_after_cycles:
                return
            await asyncio.sleep(args.poll_interval_seconds)


async def set_fast_pacing(session, seconds: int) -> None:
    await session.execute(
        update(Account).values(
            send_interval_seconds=seconds,
            send_jitter_seconds=0,
            next_available_at=datetime.now(UTC),
        )
    )
    await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
