from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.account_sync import AccountSyncService
from app.services.crmchat_connector import CRMChatAPIError, CRMChatConnector


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync CRMChat active Telegram accounts into local DB.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--reset-expired-rate-limits", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    async with CRMChatConnector() as connector:
        async with AsyncSessionLocal() as session:
            service = AccountSyncService(session, connector=connector)
            if args.reset_expired_rate_limits:
                reset = await service.reset_expired_rate_limits()
                print(f"expired rate limits reset: {reset}")
            result = await service.sync_active_accounts(dry_run=args.dry_run)
            print(
                "account sync: "
                f"total_remote={result.total_remote} active_remote={result.active_remote} "
                f"created={result.created} updated={result.updated} dry_run={args.dry_run}"
            )
            if args.report:
                report = await service.health_report()
                print(
                    "account report: "
                    f"total={report.total} active={report.active} healthy={report.healthy} "
                    f"rate_limited={report.rate_limited} unhealthy={report.unhealthy} "
                    f"next_available={report.next_available_count}"
                )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CRMChatAPIError as exc:
        print(f"CRMchat API error: {exc}")
        raise SystemExit(1) from exc
