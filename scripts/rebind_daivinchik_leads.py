"""One-time recovery: rebind peerless Дайвинчик intake dialogs to their real
Telegram peer so the funnel can READ the girls' replies.

Background: before the lead-binding fix, `LeadIntakeService` created a peerless
placeholder dialog (`intake:daivinchik:@user`) and ran the funnel there. The opener
was delivered (resolve-at-send), but the poller can never read replies on a peerless
dialog, and `--leads-only` scope only includes `telegram:%` dialogs — so the funnel
went deaf and every Дайвинчик lead stalled at interest_check.

This script resolves each such dialog's @username -> Telegram peer (per the account
that captured it) and rewrites the SAME dialog row in place:
  crmchat_dialog_id: intake:daivinchik:@user  ->  telegram:<crmchat_acct>:user:<id>
  + telegram_peer_type/id/access_hash, status=active

Keeping the same dialog UUID preserves the funnel runtime, langgraph checkpoint and
lead row, so NO duplicate opener is sent — the dialog simply becomes readable and
enters the leads-only poll scope. Idempotent: already-telegram dialogs are skipped.

Run:  .venv\\Scripts\\python.exe scripts\\rebind_daivinchik_leads.py [--apply]
Without --apply it's a dry run (resolve + report only).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.models.dialog import Dialog
from app.services.crmchat_connector import (
    CRMChatConnector,
    build_input_peer_from_resolve_username,
)


def _out(text: str) -> None:
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist changes (default: dry run).")
    parser.add_argument("--source-prefix", default="intake:daivinchik:")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Dialog).where(
                    Dialog.crmchat_dialog_id.like(f"{args.source_prefix}%"),
                    Dialog.telegram_peer_id.is_(None),
                )
            )
        ).scalars().all()

        if not rows:
            _out("No peerless intake dialogs to rebind. Nothing to do.")
            return

        by_account: dict[str, list[Dialog]] = defaultdict(list)
        for d in rows:
            by_account[str(d.account_id)].append(d)
        _out(f"Found {len(rows)} peerless intake dialog(s) across {len(by_account)} account(s).\n")

        fixed = skipped = failed = 0
        for account_id, dialogs in by_account.items():
            account = (
                await session.execute(select(Account).where(Account.id == account_id))
            ).scalar_one_or_none()
            if account is None:
                _out(f"[account {account_id}] NOT FOUND — skipping {len(dialogs)} dialog(s)")
                failed += len(dialogs)
                continue

            async with CRMChatConnector.for_account(account) as conn:
                ctx = await conn.bootstrap()
                ws_id = ctx.workspace.id
                tg_acct = ctx.telegram_account.id
                _out(f"[account {account_id}] workspace={ws_id} tg_account={tg_acct} — {len(dialogs)} lead(s)")
                for d in dialogs:
                    username = d.telegram_username or ""
                    if not username:
                        _out(f"   - {d.crmchat_dialog_id}: no username — skip")
                        skipped += 1
                        continue
                    try:
                        resolved = await conn.resolve_username(ws_id, tg_acct, username)
                        peer = dict(build_input_peer_from_resolve_username(resolved))
                    except Exception as exc:  # noqa: BLE001
                        _out(f"   - {username}: RESOLVE FAILED ({exc}) — skip")
                        failed += 1
                        continue
                    user_id = str(peer.get("userId") or peer.get("user_id") or "")
                    access_hash = peer.get("accessHash") or peer.get("access_hash")
                    if not user_id:
                        _out(f"   - {username}: no user id in resolve — skip")
                        failed += 1
                        continue
                    canonical = f"telegram:{tg_acct}:user:{user_id}"

                    # Collision guard: a real telegram dialog for this peer already exists.
                    existing = (
                        await session.execute(
                            select(Dialog).where(Dialog.crmchat_dialog_id == canonical)
                        )
                    ).scalar_one_or_none()
                    if existing is not None and existing.id != d.id:
                        _out(f"   - {username}: canonical dialog already exists ({existing.id}) — skip rebind")
                        skipped += 1
                        continue

                    _out(f"   - {username}: {d.crmchat_dialog_id} -> {canonical}")
                    if args.apply:
                        d.crmchat_dialog_id = canonical
                        d.telegram_peer_type = "user"
                        d.telegram_peer_id = user_id
                        d.telegram_access_hash = str(access_hash) if access_hash is not None else d.telegram_access_hash
                        d.status = "active"
                    fixed += 1

        if args.apply:
            await session.commit()
            _out(f"\nAPPLIED. rebound={fixed} skipped={skipped} failed={failed}")
        else:
            _out(f"\nDRY RUN (no changes). would_rebind={fixed} skipped={skipped} failed={failed}")
            _out("Re-run with --apply to persist.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
