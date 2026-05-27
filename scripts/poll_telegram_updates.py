from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.crmchat_connector import CRMChatAPIError, CRMChatConnector
from app.services.crmchat_diagnostics import redact_value
from app.services.telegram_polling import TelegramPollingService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Telegram dialogs/messages through CRMchat and persist new messages."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep polling forever. Without this flag, runs exactly once.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Polling interval for --loop. Defaults to TELEGRAM_POLL_INTERVAL_SECONDS.",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        help="Poll every active Telegram account in the selected workspace.",
    )
    parser.add_argument(
        "--only-username",
        help="Only sync one Telegram username, for example @iamnekiy.",
    )
    parser.add_argument(
        "--mark-read",
        action="store_true",
        help="Mark synced inbound history as read. Use with --only-username for safe targeted tests.",
    )
    return parser.parse_args()


async def run() -> None:
    args = parse_args()
    settings = get_settings()
    interval = args.interval_seconds or settings.telegram_poll_interval_seconds

    async with CRMChatConnector(settings=settings) as connector:
        if args.loop:
            print(f"Starting Telegram polling loop; interval={interval}s")
            while True:
                async with AsyncSessionLocal() as session:
                    service = TelegramPollingService(
                        session=session,
                        connector=connector,
                        settings=settings,
                        only_username=args.only_username,
                        mark_read=args.mark_read,
                    )
                    result = (
                        await service.poll_all_active_accounts_once()
                        if args.all_accounts
                        else await service.poll_once()
                    )
                    print_result(result)
                    sleep_seconds = max(interval, result.flood_wait_seconds or 0)
                await asyncio.sleep(sleep_seconds)
        else:
            async with AsyncSessionLocal() as session:
                service = TelegramPollingService(
                    session=session,
                    connector=connector,
                    settings=settings,
                    only_username=args.only_username,
                    mark_read=args.mark_read,
                )
                result = (
                    await service.poll_all_active_accounts_once()
                    if args.all_accounts
                    else await service.poll_once()
                )
                print_result(result)


def print_result(result) -> None:
    print(
        "Telegram polling result: "
        f"status={result.status} dialogs_seen={result.dialogs_seen} "
        f"dialogs_synced={result.dialogs_synced} messages_seen={result.messages_seen} "
        f"messages_created={result.messages_created}"
    )
    if result.flood_wait_seconds is not None:
        print(
            f"Telegram FLOOD_WAIT: retry_after={result.flood_wait_seconds}s "
            f"next_run_at={result.next_run_at}"
        )
    if result.error_message:
        print(f"Error: {result.error_message}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except CRMChatAPIError as exc:
        print(f"CRMchat API error: {exc}")
        if exc.status_code:
            print(f"Status code: {exc.status_code}")
        if exc.payload:
            print("Redacted error payload:")
            print(json.dumps(redact_value(exc.payload), ensure_ascii=False, indent=2))
        raise SystemExit(1) from exc
