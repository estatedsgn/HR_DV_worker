"""Entry point for the Дайвинчик auto-swiper.

    python -m app.services.daivinchik              # run the 24/7 loop
    python -m app.services.daivinchik probe        # dump last messages+buttons
    python -m app.services.daivinchik probe -n 20  # ... last 20

``probe`` is the tuning tool: it prints the real messages and button layouts
the bot is sending right now plus what the classifier would decide, so the rules
can be adjusted to match reality.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.repositories.account import AccountRepository
from app.services.daivinchik.service import DaivinchikService


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Дайвинчик auto-swiper")
    sub = parser.add_subparsers(dest="command")
    probe = sub.add_parser("probe", help="Dump recent bot messages and buttons")
    probe.add_argument("-n", "--limit", type=int, default=10)
    parser.add_argument(
        "--account",
        default=None,
        help=(
            "Run as a specific account (UUID, CRMchat account id, or @username). "
            "Uses that account's own CRMchat key, per-account state file and config. "
            "Omit for the legacy single-account (.env) run."
        ),
    )
    parser.add_argument(
        "--ignore-enabled-flag",
        action="store_true",
        help="Run even if the enabled flag is false (handy for first probes).",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=None,
        help="Stop after this many real button presses (for a supervised trial run).",
    )
    parser.add_argument(
        "--stop-on-limit",
        action="store_true",
        help="Exit when the daily like limit is hit instead of pausing until tomorrow.",
    )
    return parser.parse_args()


async def _amain(args: argparse.Namespace) -> None:
    settings = get_settings()
    account = None
    if args.account:
        async with AsyncSessionLocal() as session:
            account = await AccountRepository(session).get_by_reference(args.account)
        if account is None:
            print(f"account not found: {args.account!r}")
            raise SystemExit(2)
        # detach is fine — we only read its credentials/config attributes
    service = DaivinchikService(settings, account=account)
    if args.command == "probe":
        await service.probe(limit=args.limit)
        return
    enabled = account.daivinchik_enabled if account is not None else settings.daivinchik_enabled
    if not enabled and not args.ignore_enabled_flag:
        target = f"account {args.account}" if account is not None else "DAIVINCHIK_ENABLED"
        print(f"daivinchik disabled for {target} — enable it to run the loop.")
        raise SystemExit(2)
    await service.run(max_actions=args.max_actions, stop_on_limit=args.stop_on_limit)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    args = _parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nДайвинчик-бот остановлен.")


if __name__ == "__main__":
    main()
