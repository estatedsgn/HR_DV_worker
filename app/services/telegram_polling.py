from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.account import Account
from app.models.dialog import Dialog
from app.models.message import Message
from app.models.telegram_polling_run import TelegramPollingRun
from app.repositories.account import AccountRepository
from app.repositories.dialog import DialogRepository
from app.repositories.message import MessageRepository
from app.repositories.telegram_polling_run import TelegramPollingRunRepository
from app.services.crmchat_connector import (
    CRMChatBootstrapContext,
    CRMChatConnector,
    TelegramDialogSnapshot,
    TelegramFloodWaitError,
    TelegramMessageSnapshot,
    normalize_dialogs_response,
    normalize_messages_response,
)
from app.services.crmchat_diagnostics import build_input_peer, redact_value
from app.services.inbound_pipeline import InboundPipelineService


@dataclass(slots=True, frozen=True)
class TelegramPollingResult:
    status: str
    dialogs_seen: int = 0
    dialogs_synced: int = 0
    messages_seen: int = 0
    messages_created: int = 0
    flood_wait_seconds: int | None = None
    next_run_at: datetime | None = None
    error_message: str | None = None


class TelegramPollingService:
    """Read Telegram updates through CRMchat Raw API and persist new messages.

    Polling is intentionally read-only with respect to Telegram: it only calls
    dialogs/history methods and never sends replies. New dialogs are persisted
    with `pending_review` status so later lead qualification can decide whether a
    conversation is target or non-target before the agent replies.
    """

    def __init__(
        self,
        session: AsyncSession,
        connector: CRMChatConnector | None = None,
        settings: Settings | None = None,
        only_username: str | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.connector = connector or CRMChatConnector(settings=self.settings)
        self.inbound_pipeline = InboundPipelineService(session)
        self.only_username = normalize_username(only_username) if only_username else None

    async def poll_once(self) -> TelegramPollingResult:
        started_at = datetime.now(UTC)
        run = TelegramPollingRun(status="started", started_at=started_at)
        await TelegramPollingRunRepository(self.session).add(run)
        await self.session.flush()

        try:
            context = await self.connector.bootstrap()
            await self._poll_context_into_run(context, run)
            await self.session.commit()
            return result_from_run(run)
        except TelegramFloodWaitError as exc:
            await self._mark_rate_limited(run, exc)
            await self.session.commit()
            return result_from_run(run)
        except Exception as exc:
            run.status = "failed"
            run.error_message = str(exc)
            run.finished_at = datetime.now(UTC)
            await self.session.commit()
            raise

    async def poll_all_active_accounts_once(self) -> TelegramPollingResult:
        aggregate = TelegramPollingResult(status="completed")
        context = await self.connector.bootstrap()
        accounts = await self.connector.list_telegram_accounts(context.workspace.id)
        for telegram_account in accounts:
            if telegram_account.status != "active":
                continue
            run = TelegramPollingRun(status="started", started_at=datetime.now(UTC))
            await TelegramPollingRunRepository(self.session).add(run)
            await self.session.flush()
            account_context = CRMChatBootstrapContext(
                organization=context.organization,
                workspace=context.workspace,
                telegram_account=telegram_account,
            )
            try:
                await self._poll_context_into_run(account_context, run)
            except TelegramFloodWaitError as exc:
                await self._mark_rate_limited(run, exc)
            aggregate = merge_polling_results(aggregate, result_from_run(run))
        await self.session.commit()
        return aggregate

    async def _poll_context_into_run(
        self, context: CRMChatBootstrapContext, run: TelegramPollingRun
    ) -> None:
        account = await self._get_or_create_account(context)
        run.crmchat_organization_id = context.organization.id
        run.crmchat_workspace_id = context.workspace.id
        run.crmchat_account_id = context.telegram_account.id

        dialogs_payload = await self.connector.get_dialogs(
            context.workspace.id,
            context.telegram_account.id,
            limit=self.settings.telegram_poll_dialogs_limit,
        )
        dialogs = normalize_dialogs_response(dialogs_payload)
        run.dialogs_seen = len(dialogs)

        for dialog_snapshot in dialogs:
            if not should_sync_dialog(dialog_snapshot, self.only_username):
                continue
            synced, seen, created = await self._sync_dialog(
                context, account, dialog_snapshot
            )
            run.dialogs_synced += int(synced)
            run.messages_seen += seen
            run.messages_created += created

        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.next_run_at = run.finished_at + timedelta(
            seconds=self.settings.telegram_poll_interval_seconds
        )

    async def _sync_dialog(
        self,
        context: CRMChatBootstrapContext,
        account: Account,
        dialog_snapshot: TelegramDialogSnapshot,
    ) -> tuple[bool, int, int]:
        dialog = await self._get_or_create_dialog(account, dialog_snapshot)
        if dialog.status == "ignored":
            await self.session.flush()
            return True, 0, 0
        try:
            peer = build_input_peer(dialog_snapshot.peer)
        except ValueError:
            # Keep the dialog metadata but skip history until CRMchat returns a usable accessHash.
            dialog.status = "metadata_only"
            await self.session.flush()
            return True, 0, 0

        history_payload = await self.connector.get_history(
            context.workspace.id,
            context.telegram_account.id,
            peer,
            limit=self.settings.telegram_poll_history_limit,
        )
        messages = normalize_messages_response(
            history_payload, fallback_peer=dialog_snapshot.peer
        )
        created = 0
        for message_snapshot in messages:
            created += int(
                await self._persist_message(context, dialog, message_snapshot)
            )
        return True, len(messages), created

    async def _get_or_create_account(self, context: CRMChatBootstrapContext) -> Account:
        repository = AccountRepository(self.session)
        account = await repository.get_by_crmchat_account_id(
            context.telegram_account.id
        )
        if account:
            account.crmchat_organization_id = context.organization.id
            account.crmchat_workspace_id = context.workspace.id
            account.telegram_username = context.telegram_account.username
            account.metadata_json = json.dumps(
                redact_value(context.telegram_account.raw or {}), default=str
            )
            await self.session.flush()
            return account

        account = Account(
            crmchat_account_id=context.telegram_account.id,
            crmchat_organization_id=context.organization.id,
            crmchat_workspace_id=context.workspace.id,
            telegram_username=context.telegram_account.username,
            display_name=context.telegram_account.username,
            status=context.telegram_account.status or "active",
            send_interval_seconds=self.settings.outbound_default_send_interval_seconds,
            send_jitter_seconds=self.settings.outbound_default_send_jitter_seconds,
            metadata_json=json.dumps(
                redact_value(context.telegram_account.raw or {}), default=str
            ),
        )
        return await repository.add(account)

    async def _get_or_create_dialog(
        self, account: Account, dialog_snapshot: TelegramDialogSnapshot
    ) -> Dialog:
        repository = DialogRepository(self.session)
        crmchat_dialog_id = build_dialog_external_id(
            account.crmchat_account_id, dialog_snapshot
        )
        dialog = await repository.get_by_crmchat_dialog_id(crmchat_dialog_id)
        if not dialog:
            dialog = await repository.get_by_telegram_peer(
                dialog_snapshot.peer.peer_type, dialog_snapshot.peer.peer_id
            )
        if dialog:
            update_dialog_from_snapshot(dialog, dialog_snapshot)
            await self.session.flush()
            return dialog

        dialog = Dialog(
            account_id=account.id,
            crmchat_dialog_id=crmchat_dialog_id,
            lead_external_id=dialog_snapshot.peer.peer_id,
            status=initial_dialog_status(dialog_snapshot),
        )
        update_dialog_from_snapshot(dialog, dialog_snapshot)
        return await repository.add(dialog)

    async def _persist_message(
        self,
        context: CRMChatBootstrapContext,
        dialog: Dialog,
        message_snapshot: TelegramMessageSnapshot,
    ) -> bool:
        repository = MessageRepository(self.session)
        external_message_id = build_message_external_id(
            context.workspace.id,
            context.telegram_account.id,
            dialog.telegram_peer_type or "unknown",
            dialog.telegram_peer_id or "unknown",
            message_snapshot.message_id,
        )
        if await repository.get_by_crmchat_message_id(external_message_id):
            return False

        message_kwargs = {
            "dialog_id": dialog.id,
            "crmchat_message_id": external_message_id,
            "direction": "outbound" if message_snapshot.outgoing else "inbound",
            "sender_type": "account" if message_snapshot.outgoing else "lead",
            "body": message_snapshot.text or "",
            "status": "synced",
        }
        sent_at = parse_telegram_datetime(message_snapshot.date)
        if sent_at is not None:
            message_kwargs["sent_at"] = sent_at
        message = Message(**message_kwargs)
        await repository.add(message)
        await self.inbound_pipeline.enqueue(
            source="telegram_polling",
            payload={
                "workspace_id": context.workspace.id,
                "account_id": context.telegram_account.id,
                "dialog_id": str(dialog.id),
                "db_message_id": str(message.id),
                "message_id": message_snapshot.message_id,
                "text": message_snapshot.text or "",
                "outgoing": message_snapshot.outgoing,
            },
            external_message_id=external_message_id,
            account_id=dialog.account_id,
            dialog_id=dialog.id,
        )
        return True

    async def _mark_rate_limited(
        self, run: TelegramPollingRun, exc: TelegramFloodWaitError
    ) -> None:
        now = datetime.now(UTC)
        retry_after = max(
            exc.retry_after_seconds, self.settings.telegram_poll_interval_seconds
        )
        run.status = "rate_limited"
        run.flood_wait_seconds = exc.retry_after_seconds
        run.error_message = str(exc)
        run.finished_at = now
        run.next_run_at = now + timedelta(seconds=retry_after)


async def run_polling_loop(
    service: TelegramPollingService,
    *,
    interval_seconds: int,
    stop_after_runs: int | None = None,
) -> None:
    runs = 0
    while True:
        result = await service.poll_once()
        runs += 1
        if stop_after_runs is not None and runs >= stop_after_runs:
            return
        sleep_seconds = interval_seconds
        if result.flood_wait_seconds is not None:
            sleep_seconds = max(interval_seconds, result.flood_wait_seconds)
        await asyncio.sleep(sleep_seconds)


def initial_dialog_status(dialog_snapshot: TelegramDialogSnapshot) -> str:
    if dialog_snapshot.peer.peer_type != "user":
        return "ignored"
    raw_user = (dialog_snapshot.peer.raw or {}).get("user")
    if isinstance(raw_user, dict) and raw_user.get("bot"):
        return "ignored"
    return "pending_review"


def should_sync_dialog(
    dialog_snapshot: TelegramDialogSnapshot, only_username: str | None = None
) -> bool:
    if only_username is None:
        return True
    return normalize_username(dialog_snapshot.peer.username) == only_username


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    username = value.strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username or None


def update_dialog_from_snapshot(
    dialog: Dialog, dialog_snapshot: TelegramDialogSnapshot
) -> None:
    dialog.telegram_peer_type = dialog_snapshot.peer.peer_type
    dialog.telegram_peer_id = dialog_snapshot.peer.peer_id
    dialog.telegram_access_hash = dialog_snapshot.peer.access_hash
    dialog.telegram_username = dialog_snapshot.peer.username
    if dialog_snapshot.peer.display_name:
        dialog.memory_summary = (
            f"Telegram dialog with {dialog_snapshot.peer.display_name}"
        )


def build_dialog_external_id(
    crmchat_account_id: str, dialog_snapshot: TelegramDialogSnapshot
) -> str:
    return (
        f"telegram:{crmchat_account_id}:"
        f"{dialog_snapshot.peer.peer_type}:{dialog_snapshot.peer.peer_id}"
    )


def build_message_external_id(
    workspace_id: str,
    account_id: str,
    peer_type: str,
    peer_id: str,
    message_id: str,
) -> str:
    return f"telegram:{workspace_id}:{account_id}:{peer_type}:{peer_id}:{message_id}"


def parse_telegram_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.isdigit():
        return datetime.fromtimestamp(int(value), UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def result_from_run(run: TelegramPollingRun) -> TelegramPollingResult:
    return TelegramPollingResult(
        status=run.status,
        dialogs_seen=run.dialogs_seen,
        dialogs_synced=run.dialogs_synced,
        messages_seen=run.messages_seen,
        messages_created=run.messages_created,
        flood_wait_seconds=run.flood_wait_seconds,
        next_run_at=run.next_run_at,
        error_message=run.error_message,
    )


def merge_polling_results(
    left: TelegramPollingResult, right: TelegramPollingResult
) -> TelegramPollingResult:
    status = "completed" if left.status == right.status == "completed" else right.status
    return TelegramPollingResult(
        status=status,
        dialogs_seen=left.dialogs_seen + right.dialogs_seen,
        dialogs_synced=left.dialogs_synced + right.dialogs_synced,
        messages_seen=left.messages_seen + right.messages_seen,
        messages_created=left.messages_created + right.messages_created,
        flood_wait_seconds=right.flood_wait_seconds or left.flood_wait_seconds,
        next_run_at=right.next_run_at or left.next_run_at,
        error_message=right.error_message or left.error_message,
    )
