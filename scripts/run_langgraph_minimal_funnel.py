from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the minimal LangGraph HR funnel autonomously for one Telegram username."
    )
    parser.add_argument("--username", default="@iamnekiy")
    parser.add_argument("--allow-real-send", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--cycles", type=int, default=80)
    parser.add_argument("--poll-interval-seconds", type=float, default=3.0)
    parser.add_argument("--outbound-limit", type=int, default=5)
    parser.add_argument("--inbound-limit", type=int, default=50)
    parser.add_argument("--typing-delay-seconds", type=float, default=1.0)
    parser.add_argument("--step-timeout-seconds", type=float, default=45.0)
    parser.add_argument("--report-path")
    return parser.parse_args()


def normalize_username(value: str | None) -> str:
    username = (value or "").strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


def json_default(value: Any) -> str:
    return str(value)


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )


async def main() -> None:
    args = parse_args()
    os.environ.setdefault("LANGGRAPH_FUNNEL_ENABLED", "true")
    os.environ.setdefault("BRAIN_SHADOW_MODE", "false")
    os.environ.setdefault("OUTBOUND_ALLOWED_USERNAMES", args.username)

    from app.core.config import get_settings
    from app.db.session import AsyncSessionLocal
    from app.models.dialog import Dialog
    from app.models.funnel_graph import LeadFunnelRuntime
    from app.models.inbound_event import InboundEvent
    from app.models.lead import Lead
    from app.models.message import Message
    from app.models.outbound_job import OutboundJob
    from app.services.crmchat_connector import CRMChatConnector
    from app.services.funnel_graph.gateway import LangGraphFunnelGateway
    from app.services.inbound_queue_worker import InboundQueueWorker
    from app.services.outbound_queue_worker import OutboundQueueWorker
    from app.services.telegram_polling import TelegramPollingService

    settings = get_settings()
    username = normalize_username(args.username)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = Path(
        args.report_path
        or f"runtime_logs/langgraph_minimal_funnel_{username.lstrip('@')}_{timestamp}.json"
    )
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC),
        "username": username,
        "report_path": str(report_path),
        "reset": args.reset,
        "allow_real_send": args.allow_real_send,
        "cycles_requested": args.cycles,
        "events": [],
        "snapshots": [],
        "stop_reason": None,
    }
    write_report(report_path, report)

    async def run_step(label: str, awaitable):
        report["events"].append({"type": "step_started", "step": label, "at": datetime.now(UTC)})
        write_report(report_path, report)
        try:
            result = await asyncio.wait_for(awaitable, timeout=max(1.0, args.step_timeout_seconds))
        except Exception as exc:
            report["events"].append(
                {
                    "type": "step_failed",
                    "step": label,
                    "at": datetime.now(UTC),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            report["stop_reason"] = f"step_failed_{label}"
            report["finished_at"] = datetime.now(UTC)
            write_report(report_path, report)
            raise
        report["events"].append({"type": "step_completed", "step": label, "at": datetime.now(UTC)})
        write_report(report_path, report)
        return result

    async def dialogs_for_username(session) -> list[Dialog]:
        result = await session.execute(
            select(Dialog)
            .where(func.lower(func.replace(Dialog.telegram_username, "@", "")) == username.lstrip("@"))
            .order_by(Dialog.updated_at.desc(), Dialog.created_at.desc())
        )
        return list(result.scalars().all())

    async def canonical_dialog(session) -> Dialog | None:
        dialogs = await dialogs_for_username(session)
        telegram_dialogs = [
            dialog
            for dialog in dialogs
            if str(dialog.crmchat_dialog_id or "").startswith("telegram:")
            and dialog.telegram_peer_type == "user"
        ]
        if telegram_dialogs:
            return telegram_dialogs[0]
        return dialogs[0] if dialogs else None

    async def get_or_create_lead(session, dialog: Dialog) -> Lead:
        result = await session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        lead = result.scalar_one_or_none()
        if lead is not None:
            return lead
        lead = Lead(dialog_id=dialog.id, qualification_status="new", funnel_state="NEW_LEAD")
        session.add(lead)
        await session.flush()
        return lead

    async def get_or_create_runtime(session, lead: Lead, dialog: Dialog) -> LeadFunnelRuntime:
        result = await session.execute(
            select(LeadFunnelRuntime).where(LeadFunnelRuntime.lead_id == lead.id).limit(1)
        )
        runtime = result.scalar_one_or_none()
        if runtime is not None:
            return runtime
        runtime = LeadFunnelRuntime(
            lead_id=lead.id,
            dialog_id=dialog.id,
            thread_id=str(dialog.id),
            stage="new",
            status="active",
            current_goal="minimal HR intro funnel",
            metadata_json={},
        )
        session.add(runtime)
        await session.flush()
        return runtime

    async def reset_for_new_run(session, dialog: Dialog) -> LeadFunnelRuntime:
        dialogs = await dialogs_for_username(session)
        dialog_ids = [item.id for item in dialogs]
        now = datetime.now(UTC)
        if dialog_ids:
            jobs = await session.execute(
                select(OutboundJob).where(
                    OutboundJob.dialog_id.in_(dialog_ids),
                    OutboundJob.status.in_(["queued", "retry", "processing"]),
                )
            )
            for job in jobs.scalars():
                job.status = "cancelled"
                job.next_attempt_at = None
                job.lease_owner = None
                job.lease_expires_at = None
                job.error_message = "cancelled by langgraph minimal funnel reset"

            events = await session.execute(
                select(InboundEvent).where(
                    InboundEvent.dialog_id.in_(dialog_ids),
                    InboundEvent.status.in_(["received", "queued", "retry", "processing"]),
                )
            )
            for event in events.scalars():
                event.status = "processed"
                event.processed_at = now
                event.next_attempt_at = None
                event.lease_owner = None
                event.lease_expires_at = None
                event.error_message = "ignored by langgraph minimal funnel reset"

        lead = await get_or_create_lead(session, dialog)
        runtime = await get_or_create_runtime(session, lead, dialog)
        runtime.dialog_id = dialog.id
        runtime.thread_id = f"{dialog.id}:run-{timestamp}"
        runtime.stage = "new"
        runtime.status = "active"
        runtime.current_goal = "minimal HR intro funnel"
        runtime.last_processed_message_id = None
        runtime.metadata_json = {
            "manual_test_run": timestamp,
            "reset_at": now.isoformat(),
            "telegram_username": username,
        }
        await session.flush()
        return runtime

    async def snapshot(session, label: str) -> dict[str, Any]:
        dialog = await canonical_dialog(session)
        if dialog is None:
            return {"label": label, "dialog": None}
        lead_result = await session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
        lead = lead_result.scalar_one_or_none()
        runtime = None
        if lead is not None:
            runtime_result = await session.execute(
                select(LeadFunnelRuntime).where(LeadFunnelRuntime.lead_id == lead.id).limit(1)
            )
            runtime = runtime_result.scalar_one_or_none()
        job_result = await session.execute(
            select(OutboundJob)
            .where(OutboundJob.dialog_id == dialog.id)
            .order_by(OutboundJob.created_at.desc())
            .limit(10)
        )
        message_result = await session.execute(
            select(Message)
            .where(Message.dialog_id == dialog.id)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        event_result = await session.execute(
            select(InboundEvent)
            .where(InboundEvent.dialog_id == dialog.id)
            .order_by(InboundEvent.created_at.desc())
            .limit(10)
        )
        return {
            "label": label,
            "at": datetime.now(UTC),
            "dialog": {
                "id": str(dialog.id),
                "crmchat_dialog_id": dialog.crmchat_dialog_id,
                "username": dialog.telegram_username,
                "peer_type": dialog.telegram_peer_type,
                "peer_id": dialog.telegram_peer_id,
                "status": dialog.status,
            },
            "runtime": None
            if runtime is None
            else {
                "id": str(runtime.id),
                "thread_id": runtime.thread_id,
                "stage": runtime.stage,
                "status": runtime.status,
                "last_processed_message_id": str(runtime.last_processed_message_id)
                if runtime.last_processed_message_id
                else None,
                "metadata_json": runtime.metadata_json,
            },
            "jobs": [
                {
                    "id": str(job.id),
                    "status": job.status,
                    "text": job.text,
                    "attempt_count": job.attempt_count,
                    "next_attempt_at": job.next_attempt_at,
                    "sent_at": job.sent_at,
                    "last_error_type": job.last_error_type,
                    "error_message": job.error_message,
                }
                for job in job_result.scalars()
            ],
            "messages": [
                {
                    "id": str(message.id),
                    "direction": message.direction,
                    "status": message.status,
                    "body": message.body,
                    "created_at": message.created_at,
                    "sent_at": message.sent_at,
                }
                for message in message_result.scalars()
            ],
            "inbound_events": [
                {
                    "id": str(event.id),
                    "status": event.status,
                    "attempt_count": event.attempt_count,
                    "external_message_id": event.external_message_id,
                    "payload": event.payload,
                    "error_message": event.error_message,
                    "last_error_type": event.last_error_type,
                }
                for event in event_result.scalars()
            ],
        }

    async def has_active_jobs(session, dialog_id) -> bool:
        result = await session.execute(
            select(OutboundJob.id)
            .where(
                OutboundJob.dialog_id == dialog_id,
                OutboundJob.status.in_(["queued", "retry", "processing"]),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async with CRMChatConnector(settings=settings) as connector:
        async with AsyncSessionLocal() as session:
            dialog = await canonical_dialog(session)
            if dialog is None:
                poller = TelegramPollingService(
                    session,
                    connector=connector,
                    settings=settings,
                    only_username=username,
                    mark_read=False,
                )
                result = await run_step("initial_poll", poller.poll_once())
                report["events"].append({"type": "initial_poll", "result": result})
            else:
                report["events"].append(
                    {
                        "type": "existing_dialog",
                        "dialog_id": str(dialog.id),
                        "crmchat_dialog_id": dialog.crmchat_dialog_id,
                    }
                )
                write_report(report_path, report)

        async with AsyncSessionLocal() as session:
            dialog = await canonical_dialog(session)
            if dialog is None:
                report["stop_reason"] = "telegram_dialog_not_found"
                report["finished_at"] = datetime.now(UTC)
                write_report(report_path, report)
                return
            if args.reset:
                runtime = await reset_for_new_run(session, dialog)
                await session.commit()
                report["events"].append(
                    {
                        "type": "reset",
                        "dialog_id": str(dialog.id),
                        "thread_id": runtime.thread_id,
                    }
                )
            else:
                lead = await get_or_create_lead(session, dialog)
                runtime = await get_or_create_runtime(session, lead, dialog)
                await session.commit()
                report["events"].append(
                    {
                        "type": "loaded_runtime",
                        "dialog_id": str(dialog.id),
                        "thread_id": runtime.thread_id,
                    }
                )

        async with AsyncSessionLocal() as session:
            dialog = await canonical_dialog(session)
            if dialog is not None:
                result = await run_step(
                    "start_graph",
                    LangGraphFunnelGateway(session, settings=settings).start_for_dialog(
                        dialog_id=str(dialog.id)
                    ),
                )
                await session.commit()
                report["events"].append({"type": "start_graph", "result": result})
                report["snapshots"].append(await snapshot(session, "after_start_graph"))
                write_report(report_path, report)

        for cycle in range(1, args.cycles + 1):
            async with AsyncSessionLocal() as session:
                poll_result = await run_step(
                    f"cycle_{cycle}_poll",
                    TelegramPollingService(
                        session,
                        connector=connector,
                        settings=settings,
                        only_username=username,
                        mark_read=False,
                    ).poll_once(),
                )
            async with AsyncSessionLocal() as session:
                inbound_result = await run_step(
                    f"cycle_{cycle}_inbound",
                    InboundQueueWorker(
                        session,
                        lease_owner=f"langgraph-funnel-{timestamp}",
                        lease_seconds=60,
                    ).process_queued_batch(limit=args.inbound_limit),
                )
            async with AsyncSessionLocal() as session:
                outbound_result = await run_step(
                    f"cycle_{cycle}_outbound",
                    OutboundQueueWorker(
                        session,
                        connector=connector,
                        settings=settings,
                        lease_owner=f"langgraph-funnel-{timestamp}",
                        lease_seconds=60,
                        allow_real_send=args.allow_real_send,
                        typing_delay_seconds=args.typing_delay_seconds,
                    ).process_queued_batch(limit=args.outbound_limit),
                )
            async with AsyncSessionLocal() as session:
                current = await snapshot(session, f"cycle_{cycle}")
                report["snapshots"].append(current)
                report["events"].append(
                    {
                        "type": "cycle",
                        "cycle": cycle,
                        "poll": poll_result,
                        "inbound": inbound_result,
                        "outbound": outbound_result,
                    }
                )
                dialog_info = current.get("dialog") or {}
                runtime_info = current.get("runtime") or {}
                dialog_id = dialog_info.get("id")
                active_jobs = False
                if dialog_id:
                    from uuid import UUID

                    active_jobs = await has_active_jobs(session, UUID(dialog_id))
                stage = runtime_info.get("stage")
                status = runtime_info.get("status")
                if stage == "age_check" and not active_jobs:
                    report["stop_reason"] = "age_check_reached_minimal_funnel_complete"
                elif stage in {"lost", "handoff", "closed"} and not active_jobs:
                    report["stop_reason"] = f"terminal_stage_{stage}"
                elif status in {"closed", "handoff"} and not active_jobs:
                    report["stop_reason"] = f"terminal_status_{status}"
                write_report(report_path, report)
                if report["stop_reason"]:
                    break
            await asyncio.sleep(args.poll_interval_seconds)

    if report["stop_reason"] is None:
        report["stop_reason"] = "cycles_exhausted"
    report["finished_at"] = datetime.now(UTC)
    write_report(report_path, report)
    print(f"report_path={report_path}")
    print(f"stop_reason={report['stop_reason']}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
