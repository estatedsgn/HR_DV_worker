"""Recover Дайвинчик mutual matches that the live swiper missed.

Why this exists: the swiper only sweeps the last ``DAIVINCHIK_HISTORY_LIMIT``
messages and advances ``last_match_id`` past whatever it scanned. If a burst of
matches arrives while the daily lead cap is reached, the later matches scroll out
of that small window before the cap resets, and ``last_match_id`` jumps past them
on the next sweep — so they are silently dropped and never enter the funnel.

This one-off scans a LARGE history window of the bot dialog for each account,
re-runs the SAME production classifier to find every mutual match, dedupes against
already-captured leads (by @username), and enqueues the missing ones through
``LeadIntakeService`` WITH the per-account connector — so each recovered lead is
bound to its real Telegram peer and gets the funnel opener, exactly like a fresh
capture. Idempotent: intake dedupes on (source, external_lead_id), so re-runs and
already-captured matches are no-ops.

Run:  .venv\\Scripts\\python.exe scripts\\recover_daivinchik_matches.py [--apply]
       [--limit 200] [--max-new 20]
Dry run (default) only reports what it would recover.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.models.lead_intake_event import LeadIntakeEvent
from app.services.crmchat_connector import (
    CRMChatConnector,
    build_input_peer_from_resolve_username,
)
from app.services.daivinchik.classifier import classify
from app.services.daivinchik.keyboards import parse_bot_message
from app.services.crmchat_connector import normalize_messages_response
from app.services.lead_intake import LeadIntakeService


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def _retry(coro_factory, *, attempts: int = 6, base_delay: float = 3.0):
    """Run an async call, retrying through the intermittent api.crmchat.ai
    ConnectError/ReadTimeout flakiness (same transient the autopilot retries)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            _out(f"    (network retry {i + 1}/{attempts}: {type(exc).__name__})")
            await asyncio.sleep(base_delay * (i + 1))
    raise last  # type: ignore[misc]


async def _captured_usernames(session) -> set[str]:
    rows = await session.execute(
        select(func.lower(func.replace(LeadIntakeEvent.telegram_username, "@", "")))
        .where(LeadIntakeEvent.source == "daivinchik", LeadIntakeEvent.telegram_username.isnot(None))
    )
    return {value for (value,) in rows.fetchall() if value}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist (default: dry run).")
    parser.add_argument("--limit", type=int, default=200, help="History window to scan per account.")
    parser.add_argument("--max-new", type=int, default=20, help="Cap recovered leads per account (anti-spam).")
    args = parser.parse_args()
    settings = get_settings()
    source = settings.daivinchik_lead_source

    async with AsyncSessionLocal() as session:
        accounts = (
            await session.execute(select(Account).where(Account.daivinchik_enabled.is_(True)))
        ).scalars().all()
        if not accounts:
            _out("No daivinchik-enabled accounts.")
            return
        already = await _captured_usernames(session)

    total_new = 0
    for account in accounts:
        async with CRMChatConnector.for_account(account) as conn:
            ctx = await _retry(conn.bootstrap)
            resolved = await _retry(
                lambda: conn.resolve_username(ctx.workspace.id, ctx.telegram_account.id, settings.daivinchik_bot_username)
            )
            peer = dict(build_input_peer_from_resolve_username(resolved))
            # getHistory returns at most ~100 per call, so page backwards with
            # add_offset until we've scanned --limit messages (the cap backlog can
            # sit a few hundred messages deep).
            snapshots = []
            page_size = 100
            scanned = 0
            while scanned < args.limit:
                want = min(page_size, args.limit - scanned)
                offset = scanned
                payload = await _retry(
                    lambda w=want, o=offset: conn.get_history(
                        ctx.workspace.id, ctx.telegram_account.id, peer, limit=w, add_offset=o
                    )
                )
                page = normalize_messages_response(payload)
                if not page:
                    break
                snapshots.extend(page)
                scanned += len(page)
                if len(page) < want:
                    break
            matches: dict[str, str] = {}  # username(norm) -> external_id
            for snap in snapshots:
                if snap.raw is None:
                    continue
                msg = parse_bot_message(snap.raw)
                if msg is None:
                    continue
                decision = classify(msg)
                if decision.intent == "match" and decision.lead is not None and decision.lead.telegram_username:
                    norm = decision.lead.telegram_username.lstrip("@").lower()
                    matches.setdefault(norm, decision.lead.external_id)

            new_users = [u for u in matches if u not in already]
            label = settings.label_for_account(account.id, account.crmchat_account_id) or account.crmchat_account_id
            _out(f"[{label}] scanned {len(snapshots)} msgs, {len(matches)} match(es), {len(new_users)} NEW (uncaptured)")
            for u in new_users:
                _out(f"    + @{u}")

            if args.apply and new_users:
                to_apply = new_users[: args.max_new]
                for u in to_apply:
                    username = f"@{u}"
                    async with AsyncSessionLocal() as session:
                        result = await LeadIntakeService(session, connector=conn).enqueue_lead(
                            source=source,
                            external_lead_id=matches[u],
                            telegram_username=username,
                            payload={"recovered": True, "via": "recover_daivinchik_matches"},
                            account_id=str(account.id),
                        )
                        await session.commit()
                    already.add(u)
                    total_new += 1
                    _out(f"    captured {username} (idempotent={result.idempotent})")
                if len(new_users) > args.max_new:
                    _out(f"    ...capped at {args.max_new}; re-run to capture the rest")

    if args.apply:
        _out(f"\nAPPLIED. recovered={total_new}")
    else:
        _out("\nDRY RUN. Re-run with --apply to capture the NEW matches above.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
