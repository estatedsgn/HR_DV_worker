"""Переобработать застрявшие lead_intake_events (status='failed') в живых лидов.

Зачем: если свайпер поймал мэтч РАНЬШЕ, чем аккаунт был засинкан в БД, интейк
падает со status='failed' ("No active healthy account is available"), и лид так и
не создаётся. Уникальный constraint (source, external_lead_id) не даёт завести его
повторным INSERT'ом. Этот скрипт берёт существующие failed-события и доводит их до
лида НА МЕСТЕ (failed→accepted + диалог + воронка), привязывая к реальному
Telegram-пиру — точно как при свежем захвате. Идемпотентно: уже обработанные
события (accepted) не трогаются.

Запуск:
    .venv/bin/python scripts/reprocess_failed_intakes.py            # dry-run (только показать)
    .venv/bin/python scripts/reprocess_failed_intakes.py --apply    # реально завести
    .venv/bin/python scripts/reprocess_failed_intakes.py --apply --source daivinchik
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.models.lead_intake_event import LeadIntakeEvent
from app.services.crmchat_connector import CRMChatConnector
from app.services.lead_intake import LeadIntakeService


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist (default: dry run).")
    parser.add_argument("--source", default="daivinchik", help="Intake source to reprocess.")
    parser.add_argument("--limit", type=int, default=100, help="Max events to reprocess.")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(LeadIntakeEvent.id, LeadIntakeEvent.telegram_username)
            .where(
                LeadIntakeEvent.source == args.source,
                LeadIntakeEvent.status == "failed",
            )
            .order_by(LeadIntakeEvent.created_at)
            .limit(args.limit)
        )
        pending = rows.all()
        account = (
            await session.execute(
                select(Account).where(Account.daivinchik_enabled.is_(True))
            )
        ).scalars().first()
        if account is None:
            account = (
                await session.execute(select(Account).where(Account.status == "active"))
            ).scalars().first()
        account_id = account.id if account else None

    _out(f"failed '{args.source}' events to reprocess: {len(pending)}")
    for _eid, username in pending:
        _out(f"    - {username}")
    if account_id is None:
        _out("No account available — sync accounts first (scripts/sync_crmchat_accounts.py).")
        return
    if not args.apply:
        _out("\nDRY RUN. Re-run with --apply to materialize these leads.")
        return

    done = 0
    async with CRMChatConnector.for_account(account) as conn:
        for eid, username in pending:
            async with AsyncSessionLocal() as session:
                event = await session.get(LeadIntakeEvent, eid)
                acct = await session.get(Account, account_id)
                if event is None or acct is None:
                    continue
                try:
                    msg = await LeadIntakeService(session, connector=conn).reprocess_event(event, acct)
                    done += 1
                    _out(f"    reprocessed {username} -> first_message={msg!r}")
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    _out(f"    FAILED {username}: {type(exc).__name__}: {exc}")

    _out(f"\nAPPLIED. reprocessed={done}/{len(pending)}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
