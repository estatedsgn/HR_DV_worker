"""Hand a handed-off / paused dialog back to the bot.

While a dialog is human-controlled the bot stays completely silent (see
``app.services.funnel_graph.gateway.is_bot_silenced``): it never replies to the
lead, so the recruiter can run the conversation manually after a handoff. This
script reverses that — it resumes the funnel so the bot starts answering again.

    .venv\\Scripts\\python.exe scripts\\resume_funnel_dialog.py --username @user
    .venv\\Scripts\\python.exe scripts\\resume_funnel_dialog.py --dialog-id <uuid> --stage interest_check

By default it resumes at ``interest_check``; pass ``--stage`` to pick where the
bot should pick the conversation back up. Without ``--apply`` it's a dry run.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.human_handoff import HumanHandoff
from app.models.lead import Lead


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def _find_dialog(session, *, dialog_id: str | None, username: str | None) -> Dialog | None:
    if dialog_id:
        return await session.get(Dialog, dialog_id)
    handle = (username or "").lstrip("@")
    if not handle:
        return None
    result = await session.execute(
        select(Dialog)
        .where(func.lower(Dialog.telegram_username) == f"@{handle}".lower())
        .order_by(Dialog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Resume a handed-off/paused funnel dialog (give it back to the bot).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--username", help="Lead's Telegram @username.")
    group.add_argument("--dialog-id", help="Dialog UUID.")
    parser.add_argument("--stage", default="interest_check", help="Stage to resume the bot at (default: interest_check).")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry run).")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        dialog = await _find_dialog(session, dialog_id=args.dialog_id, username=args.username)
        if dialog is None:
            _out("Dialog not found.")
            raise SystemExit(2)
        lead = (
            await session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        ).scalar_one_or_none()
        if lead is None:
            _out(f"No lead for dialog {dialog.id} ({dialog.telegram_username}).")
            raise SystemExit(2)
        runtime = (
            await session.execute(
                select(LeadFunnelRuntime).where(LeadFunnelRuntime.lead_id == lead.id).limit(1)
            )
        ).scalar_one_or_none()
        if runtime is None:
            _out(f"No funnel runtime for dialog {dialog.id} ({dialog.telegram_username}).")
            raise SystemExit(2)

        metadata = dict(runtime.metadata_json or {})
        _out(
            f"Dialog {dialog.id} ({dialog.telegram_username})\n"
            f"  current: stage={runtime.stage!r} status={runtime.status!r} "
            f"bot_paused={metadata.get('bot_paused')!r}"
        )

        if not args.apply:
            _out(
                f"DRY RUN — would set stage={args.stage!r}, status='active', clear bot_paused, "
                f"close open handoff. Re-run with --apply to do it."
            )
            return

        runtime.stage = args.stage
        runtime.status = "active"
        metadata.pop("bot_paused", None)
        metadata["resumed_by_human_at"] = datetime.now(UTC).isoformat()
        runtime.metadata_json = metadata

        open_handoffs = (
            await session.execute(
                select(HumanHandoff).where(
                    HumanHandoff.dialog_id == dialog.id, HumanHandoff.status == "open"
                )
            )
        ).scalars().all()
        for handoff in open_handoffs:
            handoff.status = "resolved"
            handoff.resolved_at = datetime.now(UTC)

        await session.commit()
        _out(
            f"RESUMED — stage={runtime.stage!r}, status='active', bot_paused cleared, "
            f"{len(open_handoffs)} handoff record(s) closed. The bot will reply again on the next inbound."
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
