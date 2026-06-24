"""Detect leads who (likely) blocked our account.

Telegram does not expose a clean "this user blocked you" flag to a userbot.
The practical tell is the contact's *last seen* status: a user who blocked us
shows up as "last online a long time ago" / hidden, even though they were
clearly online while chatting with us.

This probe resolves each lead via contacts.resolveUsername (read-only), reads
the returned user `status`, and cross-checks it against the timestamp of their
last inbound message to us. The cross-check is what separates a real block from
someone who merely hid their last-seen via privacy settings:

    last-seen "a long time ago"  +  they messaged us recently  =  almost surely a block
    (you cannot have been "last online a month ago" if you wrote to us yesterday)

Usage (read-only, safe):
    python scripts/detect_blocks.py                 # all active leads, all accounts
    python scripts/detect_blocks.py --account <id>  # one account only
    python scripts/detect_blocks.py --limit 30      # cap how many to resolve
    python scripts/detect_blocks.py --all-stages    # include lost/handoff too
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from sqlalchemy import select, text  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.account import Account  # noqa: E402
from app.services.crmchat_connector import (  # noqa: E402
    CRMChatConnector,
    TelegramFloodWaitError,
)

# How recently they must have messaged us for a "long ago" last-seen to count
# as a contradiction (i.e. a likely block) rather than a long-dead lead.
RECENT_CONTACT_DAYS = 21
# wasOnline older than this (and recent contact) => strong block signal.
STALE_LAST_SEEN_DAYS = 14

CANDIDATE_SQL = """
SELECT d.id::text          AS dialog_id,
       d.account_id::text  AS account_id,
       d.telegram_username AS username,
       d.status            AS dialog_status,
       r.stage             AS stage,
       (SELECT max(m.created_at) FROM messages m
          WHERE m.dialog_id = d.id AND m.direction = 'inbound')  AS last_inbound,
       (SELECT max(m.created_at) FROM messages m
          WHERE m.dialog_id = d.id AND m.direction = 'outbound') AS last_outbound
FROM dialogs d
LEFT JOIN lead_funnel_runtime r ON r.dialog_id = d.id
WHERE d.telegram_username IS NOT NULL
  AND d.crmchat_dialog_id LIKE 'telegram:%'
  {stage_filter}
ORDER BY last_inbound DESC NULLS LAST
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_ago(ts: datetime | None) -> float | None:
    ts = _aware(ts)
    if ts is None:
        return None
    return (_now() - ts).total_seconds() / 86400.0


def _extract_user(resolved) -> dict | None:
    data = resolved.get("data") if isinstance(resolved.get("data"), dict) else resolved
    users = data.get("users") if isinstance(data, dict) else None
    if isinstance(users, list) and users and isinstance(users[0], dict):
        return users[0]
    return None


def classify(user: dict | None, last_inbound: datetime | None) -> tuple[str, str]:
    """Return (verdict, detail). verdict in {BLOCKED, SUSPECT, ACTIVE, INACTIVE, GONE}."""
    if user is None:
        return "GONE", "resolve returned no user (username changed / account deleted)"

    status = user.get("status") or {}
    stype = str(status.get("_") or "userStatusEmpty")
    inbound_age = _days_ago(last_inbound)
    chatted_recently = inbound_age is not None and inbound_age <= RECENT_CONTACT_DAYS

    if stype in ("userStatusOnline", "userStatusRecently"):
        return "ACTIVE", f"{stype} — online/recent, not blocked"

    if stype == "userStatusOffline":
        wo = status.get("wasOnline") or status.get("was_online")
        if wo:
            seen = datetime.fromtimestamp(int(wo), tz=timezone.utc)
            seen_age = _days_ago(seen)
            # Hard contradiction: last-seen predates their last message to us.
            if last_inbound is not None and seen < _aware(last_inbound):
                return (
                    "BLOCKED",
                    f"last-seen {seen:%Y-%m-%d} is BEFORE their last msg to us "
                    f"{_aware(last_inbound):%Y-%m-%d} — impossible unless blocked",
                )
            if chatted_recently and seen_age and seen_age >= STALE_LAST_SEEN_DAYS:
                return (
                    "BLOCKED",
                    f"last-seen {seen_age:.0f}d ago but chatted {inbound_age:.0f}d ago",
                )
            return "INACTIVE", f"offline, last-seen {seen_age:.0f}d ago"
        return "INACTIVE", "offline, no wasOnline timestamp"

    # userStatusLastWeek / userStatusLastMonth / userStatusEmpty => hidden granularity
    if chatted_recently:
        return (
            "SUSPECT",
            f"{stype} (hidden/long-ago) but chatted {inbound_age:.0f}d ago "
            f"— block or last-seen privacy",
        )
    return "INACTIVE", f"{stype}, no recent contact"


def _status_str(user: dict | None) -> tuple[str, str]:
    """Return (raw status type, wasOnline-or-empty)."""
    if user is None:
        return "<no user>", ""
    status = user.get("status") or {}
    stype = str(status.get("_") or "userStatusEmpty")
    wo = status.get("wasOnline") or status.get("was_online")
    if wo:
        seen = datetime.fromtimestamp(int(wo), tz=timezone.utc)
        return stype, f"{seen:%Y-%m-%d %H:%M} ({_days_ago(seen):.0f}d)"
    return stype, ""


async def probe_account(account: Account, rows: list[dict], limit: int | None):
    results = []
    async with CRMChatConnector.for_account(account) as conn:
        ctx = await conn.bootstrap()
        ws, acc_id = ctx.workspace.id, ctx.telegram_account.id
        for row in rows:
            if limit is not None and len(results) >= limit:
                break
            uname = row["username"]
            try:
                resolved = await conn.resolve_username(ws, acc_id, uname)
                user = _extract_user(resolved)
                stype, seen = _status_str(user)
                verdict, detail = classify(user, row["last_inbound"])
            except TelegramFloodWaitError as exc:
                wait = exc.retry_after_seconds + 2
                print(f"  … FLOOD_WAIT {exc.retry_after_seconds}s, sleeping", flush=True)
                await asyncio.sleep(wait)
                continue
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                stype, seen = "<resolve-error>", ""
                if any(m in msg for m in ("PEER_ID_INVALID", "USERNAME_NOT_OCCUPIED",
                                          "USERNAME_INVALID")):
                    verdict, detail = "GONE", f"resolve failed: {msg[:50]}"
                else:
                    verdict, detail = "ERROR", msg[:60]
            inbound_age = _days_ago(row["last_inbound"])
            in_str = f"{inbound_age:.0f}d" if inbound_age is not None else "never"
            results.append((verdict, uname, row["stage"], stype, seen, in_str, detail))
            # live raw line so we can eyeball the distribution as it runs
            print(f"  {(uname or '')[:20]:20} {stype:20} seen={seen:24} "
                  f"last_in={in_str:6} {(row['stage'] or '-')[:18]}", flush=True)
            await asyncio.sleep(1.5)  # gentle pacing to avoid FLOOD_WAIT
    return results


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="restrict to one DB account id")
    ap.add_argument("--limit", type=int, default=None, help="max users to resolve")
    ap.add_argument("--all-stages", action="store_true",
                    help="include lost / human_handoff (default: skip them)")
    ap.add_argument("--username", help="resolve ONE username on --account and print raw status")
    args = ap.parse_args()

    if args.username:
        if not args.account:
            print("--username requires --account"); return
        async with AsyncSessionLocal() as session:
            account = (await session.execute(
                select(Account).where(Account.id == args.account))).scalar_one_or_none()
        if account is None:
            print(f"account {args.account} not found"); return
        async with CRMChatConnector.for_account(account) as conn:
            ctx = await conn.bootstrap()
            resolved = await conn.resolve_username(
                ctx.workspace.id, ctx.telegram_account.id, args.username)
            user = _extract_user(resolved)
            stype, seen = _status_str(user)
            print(f"\nusername : {args.username}")
            print(f"status._ : {stype}")
            print(f"last-seen: {seen or '(none / hidden)'}")
            print(f"raw user : {user}")
        return

    stage_filter = ""
    if not args.all_stages:
        stage_filter = "AND (r.stage IS NULL OR r.stage NOT IN ('lost', 'human_handoff'))"
    if args.account:
        stage_filter += f"\n  AND d.account_id = '{args.account}'::uuid"
    sql = CANDIDATE_SQL.format(stage_filter=stage_filter)

    async with AsyncSessionLocal() as session:
        rows = [dict(r) for r in (await session.execute(text(sql))).mappings().all()]
        acc_ids = {r["account_id"] for r in rows}
        accounts = {
            str(a.id): a for a in
            (await session.execute(select(Account).where(Account.id.in_(acc_ids)))).scalars()
        }

    print(f"candidates: {len(rows)} leads across {len(acc_ids)} account(s)\n", flush=True)

    all_results = []
    by_account: dict[str, list[dict]] = {}
    for r in rows:
        by_account.setdefault(r["account_id"], []).append(r)
    for acc_id, acc_rows in by_account.items():
        account = accounts.get(acc_id)
        if account is None:
            continue
        all_results += await probe_account(account, acc_rows, args.limit)

    # --- raw status histogram: THE thing to eyeball (blocked vs hidden) ---
    status_hist: dict[str, int] = {}
    for _v, _u, _stage, stype, _seen, _in, _d in all_results:
        status_hist[stype] = status_hist.get(stype, 0) + 1
    print("\n=== RAW status._ distribution (what each lead actually returns) ===")
    for stype, n in sorted(status_hist.items(), key=lambda x: -x[1]):
        print(f"  {stype:22} {n}")

    # --- raw status x funnel stage ---
    print("\n=== status._ x funnel stage ===")
    cross: dict[tuple[str, str], int] = {}
    for _v, _u, stage, stype, _seen, _in, _d in all_results:
        cross[(stype, stage or "(no funnel)")] = cross.get((stype, stage or "(no funnel)"), 0) + 1
    for (stype, stage), n in sorted(cross.items(), key=lambda x: (x[0][0], -x[1])):
        print(f"  {stype:22} {stage:28} {n}")

    # --- full per-lead table, grouped by raw status ---
    all_results.sort(key=lambda x: (x[3], x[1] or ""))
    print(f"\n{'STATUS._':22} {'USERNAME':20} {'LAST-SEEN':24} {'LAST_IN':8} {'STAGE':18} VERDICT")
    print("-" * 110)
    for verdict, uname, stage, stype, seen, in_str, _detail in all_results:
        print(f"{stype:22} {(uname or '')[:20]:20} {seen:24} {in_str:8} "
              f"{(stage or '-')[:18]:18} {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
