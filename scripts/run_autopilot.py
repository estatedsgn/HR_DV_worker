from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.account import Account
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.lead_intake_event import LeadIntakeEvent
from app.repositories.account import AccountRepository
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
    parser.add_argument(
        "--account",
        default=None,
        help=(
            "Scope the whole loop to one account (UUID / CRMchat id / @username): "
            "use its own CRMchat key, poll only it, and only claim its inbound/"
            "outbound jobs. This is how each per-account process runs."
        ),
    )
    parser.add_argument("--only-username", help="Only sync one username, for example @iamnekiy.")
    parser.add_argument(
        "--leads-only",
        action="store_true",
        help=(
            "Scope polling and inbound to active funnel leads (dialogs with a "
            "LeadFunnelRuntime) plus the username allowlist. Avoids walking every "
            "dialog of the account each cycle (one get_history call per lead, not 20+). "
            "Refreshed every cycle, so new Дайвинчик leads are picked up automatically."
        ),
    )
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
    parser.add_argument(
        "--error-backoff-seconds",
        type=int,
        default=20,
        help="Sleep this long after a cycle-level CRM/network error before retrying.",
    )
    parser.add_argument("--sync-accounts-every", type=int, default=60)
    parser.add_argument("--test-fast-pacing-seconds", type=int, default=None)
    parser.add_argument("--typing-delay-seconds", type=float, default=None)
    parser.add_argument("--model-test-profile", default=None)
    parser.add_argument(
        "--min-inbound-before-handoff",
        type=int,
        default=None,
        help="Test mode: keep the brain talking until this many inbound messages are collected before handoff.",
    )
    parser.add_argument("--stop-after-cycles", type=int, default=None)
    parser.add_argument(
        "--reengage-every-cycles",
        type=int,
        default=100,
        help=(
            "Раз в сколько циклов сканировать замолчавших лидов и слать бампы "
            "(~5 минут при 3с-цикле). 0 — отключить скан в этом процессе."
        ),
    )
    parser.add_argument(
        "--ignore-active-hours",
        action="store_true",
        help=(
            "Keep running outside the configured working window "
            "(DAIVINCHIK_ACTIVE_HOURS_START..END). By default the autopilot goes "
            "fully silent outside those hours — no polling, no replies, no sends — "
            "mirroring the swiper. Use this for evals/manual runs at night."
        ),
    )
    return parser.parse_args()


def _within_active_hours(settings) -> tuple[bool, datetime, str]:
    """Сейчас ли рабочее окно агента (то же, что у свайпера Дайвинчика)."""
    tz = ZoneInfo(settings.daivinchik_timezone)
    now = datetime.now(tz)
    start = settings.daivinchik_active_hours_start
    end = settings.daivinchik_active_hours_end
    window = f"{start:02d}-{end:02d} {settings.daivinchik_timezone}"
    return (start <= now.hour < end), now, window


async def main() -> None:
    args = parse_args()
    if args.min_inbound_before_handoff is not None:
        os.environ["BRAIN_MIN_INBOUND_BEFORE_HANDOFF"] = str(
            max(0, args.min_inbound_before_handoff)
        )
    if args.model_test_profile:
        os.environ["MODEL_TEST_PROFILE"] = str(args.model_test_profile)
    if not args.allow_real_send:
        print("autopilot refused to start: pass --allow-real-send to send Telegram messages")
        raise SystemExit(2)

    # Per-account mode: resolve once, then every cycle talks to CRMchat with this
    # account's own key and only touches this account's dialogs/jobs.
    account = None
    account_id: str | None = None
    if args.account:
        async with AsyncSessionLocal() as session:
            account = await AccountRepository(session).get_by_reference(args.account)
        if account is None:
            print(f"autopilot: account not found: {args.account!r}")
            raise SystemExit(2)
        account_id = str(account.id)
        from app.services.funnel_graph.model_profiles import active_model_profile

        _profile = active_model_profile(get_settings()).name
        print(
            f"autopilot scoped to account={account.telegram_username or account.crmchat_account_id} "
            f"id={account_id} model_profile={_profile}"
        )

    cycle = 0
    only_dialog_ids: list[str] | None = None
    was_paused = False
    while True:
        cycle += 1
        started = datetime.now(UTC)
        sleep_seconds = args.poll_interval_seconds

        # Гейт рабочих часов: вне окна 10–22 (DAIVINCHIK_ACTIVE_HOURS_*) агент
        # полностью молчит — не поллит, не отвечает, не шлёт, как свайпер. Так
        # лид, написавший в 23:00, получит ответ в 10:00, а не ночью.
        if not args.ignore_active_hours:
            in_hours, now_local, window = _within_active_hours(get_settings())
            if not in_hours:
                if not was_paused:
                    print(
                        f"[{cycle}] вне рабочего окна {window} "
                        f"(сейчас {now_local:%H:%M}) — пауза до начала окна"
                    )
                    was_paused = True
                await asyncio.sleep(60)
                continue
            if was_paused:
                print(f"[{cycle}] рабочее окно {window} открыто — возобновляю")
                was_paused = False
        try:
            connector = (
                CRMChatConnector.for_account(account) if account is not None else CRMChatConnector()
            )
            async with connector:
                async with AsyncSessionLocal() as session:
                    if cycle == 1 or cycle % max(1, args.sync_accounts_every) == 0:
                        sync = await AccountSyncService(session, connector=connector).sync_active_accounts()
                        print(
                            f"[{cycle}] account_sync total_remote={sync.total_remote} "
                            f"active_remote={sync.active_remote} created={sync.created} updated={sync.updated}"
                        )
                    if args.test_fast_pacing_seconds is not None:
                        await set_fast_pacing(session, args.test_fast_pacing_seconds)

                    # Скоуп на лидов воронки: пересобираем каждый цикл, чтобы новые
                    # лиды подхватывались, а мусорные диалоги не поллились.
                    lead_usernames: set[str] | None = None
                    if args.leads_only:
                        only_dialog_ids, lead_usernames = await resolve_lead_scope(
                            session, account_id=account_id
                        )

                    polling = TelegramPollingService(
                        session,
                        connector=connector,
                        only_username=args.only_username,
                        only_usernames=lead_usernames,
                        mark_read=args.mark_read_on_poll,
                    )
                    # In per-account mode poll just this account (the connector is
                    # already bound to its key); --all-accounts is for the legacy
                    # single-key, many-telegram-accounts setup only.
                    poll_result = (
                        await polling.poll_all_active_accounts_once()
                        if args.all_accounts and account is None
                        else await polling.poll_once()
                    )
                    # Узкий автопилот (--only-username) читает из inbound-очереди
                    # только свой диалог, не вычитывая чужие/мусорные события.
                    if args.only_username and not args.leads_only and only_dialog_ids is None:
                        only_dialog_ids = await resolve_dialog_ids(session, args.only_username)
                    scope_tag = account_id or args.only_username or "all"
                    inbound_result = await InboundQueueWorker(
                        session,
                        lease_owner=f"autopilot-inbound:{scope_tag}",
                        only_dialog_ids=only_dialog_ids,
                    ).process_queued_batch(limit=args.inbound_limit)
                    recovery_result = await CampaignSequenceService(session).recover_stale_runs(
                        older_than_seconds=args.recover_older_than_seconds
                    )
                    outbound_result = await OutboundQueueWorker(
                        session,
                        connector=connector,
                        lease_owner=f"autopilot-outbound:{scope_tag}",
                        allow_real_send=True,
                        typing_delay_seconds=args.typing_delay_seconds,
                        account_id=account_id,
                    ).process_queued_batch(limit=args.outbound_limit)
                    # Догоняем замолчавших лидов (до 3 бампов с нарастающими
                    # паузами) — редким сканом, чтобы не грузить каждый цикл.
                    reengaged = 0
                    if args.reengage_every_cycles > 0 and (
                        cycle == 1 or cycle % args.reengage_every_cycles == 0
                    ):
                        from app.services.funnel_graph.reengage import ReengagementService

                        reengaged = await ReengagementService(session).run_once(
                            account_id=account_id
                        )
                    metrics = await latest_model_metrics(session, args.only_username)

                elapsed = (datetime.now(UTC) - started).total_seconds()
                print(
                    f"[{cycle}] ok elapsed={elapsed:.1f}s "
                    f"poll={poll_result.status}/created:{poll_result.messages_created} "
                    f"inbound=p:{inbound_result.processed},f:{inbound_result.failed},r:{inbound_result.retry} "
                    f"recovery={recovery_result} "
                    f"outbound=s:{outbound_result.sent},res:{outbound_result.rescheduled},f:{outbound_result.failed} "
                    f"reengaged={reengaged}"
                )
                if metrics:
                    print(f"[{cycle}] model_metrics {metrics}")
        except CRMChatAPIError as exc:
            sleep_seconds = max(args.poll_interval_seconds, args.error_backoff_seconds)
            print(f"[{cycle}] crmchat_error={exc}; retry_in={sleep_seconds}s")
        except Exception as exc:
            sleep_seconds = max(args.poll_interval_seconds, args.error_backoff_seconds)
            print(f"[{cycle}] autopilot_error={type(exc).__name__}: {exc}; retry_in={sleep_seconds}s")

        if args.stop_after_cycles is not None and cycle >= args.stop_after_cycles:
            return
        await asyncio.sleep(sleep_seconds)


async def set_fast_pacing(session, seconds: int) -> None:
    await session.execute(
        update(Account).values(
            send_interval_seconds=seconds,
            send_jitter_seconds=0,
            next_available_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def resolve_dialog_ids(session, username: str) -> list[str] | None:
    """Срезолвить username в id канонических telegram-диалогов (для скоупа inbound)."""
    normalized = username.strip().lower().lstrip("@")
    result = await session.execute(
        select(Dialog.id).where(
            func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized,
            Dialog.crmchat_dialog_id.like("telegram:%"),
        )
    )
    ids = [str(row[0]) for row in result.fetchall()]
    return ids or None


async def resolve_lead_scope(
    session, account_id: str | None = None
) -> tuple[list[str] | None, set[str] | None]:
    """Собрать КУРИРУЕМЫЙ скоуп лидов воронки для текущего цикла.

    Возвращает (dialog_ids, usernames). Скоуп = реальные лиды, которых мы ведём:
      * username-allowlist (`OUTBOUND_ALLOWED_USERNAMES`) — ручные тест-лиды;
      * лиды из разрешённых источников интейка (`OUTBOUND_ALLOWED_INTAKE_SOURCES`,
        напр. daivinchik) — подхватываются динамически, как только заведены.

    Намеренно НЕ берём «все диалоги с runtime»: мусорные демо-диалоги CRMChat
    (`*_crm` и пр.) тоже могли получить runtime в старом unscoped-прогоне, и их
    история не должна тянуться каждый цикл (по одному get_history на диалог).
    """
    settings = get_settings()
    allow = [u.strip() for u in settings.outbound_allowed_usernames.split(",") if u.strip()]
    allow_norm = {u.lstrip("@").lower() for u in allow}

    # Per-account scope: only this account's intake leads / dialogs, so account A's
    # process never polls or messages account B's leads.
    account_uuid = UUID(account_id) if account_id else None

    sources = [s.strip() for s in settings.outbound_allowed_intake_sources.split(",") if s.strip()]
    wanted = set(allow_norm)
    if sources:
        intake_filters = [
            LeadIntakeEvent.source.in_(sources),
            LeadIntakeEvent.telegram_username.isnot(None),
        ]
        if account_uuid is not None:
            intake_filters.append(LeadIntakeEvent.account_id == account_uuid)
        rows = await session.execute(
            select(func.lower(func.replace(LeadIntakeEvent.telegram_username, "@", "")))
            .where(*intake_filters)
        )
        wanted.update(value for (value,) in rows.fetchall() if value)

    dialog_ids: set[str] = set()
    usernames: set[str] = set(allow)  # allowlisted — даже если диалога ещё нет (для опенера)
    if wanted:
        dialog_filters = [
            func.lower(func.replace(Dialog.telegram_username, "@", "")).in_(list(wanted)),
            Dialog.crmchat_dialog_id.like("telegram:%"),
        ]
        if account_uuid is not None:
            dialog_filters.append(Dialog.account_id == account_uuid)
        rows = await session.execute(
            select(Dialog.id, Dialog.telegram_username).where(*dialog_filters)
        )
        for did, uname in rows.fetchall():
            dialog_ids.add(str(did))
            if uname:
                usernames.add(uname)

    return (list(dialog_ids) or None, usernames or None)


async def latest_model_metrics(session, username: str | None) -> str:
    query = select(LeadFunnelRuntime).order_by(LeadFunnelRuntime.updated_at.desc()).limit(1)
    if username:
        normalized = username.strip().lower().lstrip("@")
        dialog_result = await session.execute(
            select(Dialog.id)
            .where(
                func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized,
                Dialog.crmchat_dialog_id.like("telegram:%"),
            )
            .order_by(Dialog.updated_at.desc())
            .limit(1)
        )
        dialog_id = dialog_result.scalar_one_or_none()
        if dialog_id is None:
            return ""
        query = (
            select(LeadFunnelRuntime)
            .where(LeadFunnelRuntime.dialog_id == dialog_id)
            .order_by(LeadFunnelRuntime.updated_at.desc())
            .limit(1)
        )
    runtime = (await session.execute(query)).scalar_one_or_none()
    if runtime is None:
        return ""
    metadata = runtime.metadata_json or {}
    semantic = metadata.get("semantic_metrics") or {}
    reply = metadata.get("reply_metrics") or {}
    parts = [
        f"profile={metadata.get('model_test_profile') or semantic.get('profile') or reply.get('profile') or 'unknown'}",
        f"stage={runtime.stage}",
    ]
    if semantic:
        parts.append(
            "semantic="
            f"{semantic.get('component') or 'none'}/{semantic.get('model') or 'rules'}"
            f"/{semantic.get('latency_ms')}ms"
            f"/fast={semantic.get('fast_path')}"
        )
    if reply:
        parts.append(
            "reply="
            f"{reply.get('component') or 'none'}/{reply.get('model') or 'rules'}"
            f"/{reply.get('latency_ms')}ms"
            f"/fast={reply.get('fast_path')}"
        )
    if metadata.get("graph_total_latency_ms") is not None:
        parts.append(f"graph={metadata.get('graph_total_latency_ms')}ms")
    return " ".join(parts)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
