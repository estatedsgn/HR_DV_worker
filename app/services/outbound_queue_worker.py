from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.account import Account
from app.models.brain_v2 import LeadBrainState
from app.models.dialog import Dialog
from app.models.lead_intake_event import LeadIntakeEvent
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.models.outbound_send_log import OutboundSendLog
from app.repositories.outbound_job import OutboundJobRepository
from app.services.campaign_sequence import CampaignSequenceService
from app.services.crmchat_connector import (
    CRMChatAPIError,
    CRMChatConnector,
    TelegramFloodWaitError,
    build_input_peer_from_resolve_username,
)

logger = logging.getLogger(__name__)


# Telegram-side errors that can NEVER succeed by retrying: the recipient's own
# privacy settings forbid this account from messaging them at all (e.g. a girl who
# only accepts messages from Telegram Premium accounts). Retrying just burns cycles
# and dead-letters anyway, so we fail such jobs immediately and flag the dialog as
# uncontactable instead of churning through the full retry ladder.
_PERMANENT_SEND_ERROR_MARKERS = (
    "PRIVACY_PREMIUM_REQUIRED",
    "USER_PRIVACY_RESTRICTED",
    "YOU_BLOCKED_USER",
    "USER_IS_BLOCKED",
    "PEER_ID_INVALID",
    "USER_DEACTIVATED",
    "INPUT_USER_DEACTIVATED",
)


@dataclass(slots=True, frozen=True)
class OutboundQueueBatchResult:
    total: int
    sent: int
    blocked: int
    retry: int
    failed: int
    dead_letter: int
    rescheduled: int
    cancelled: int = 0


class OutboundSendBlockedError(Exception):
    pass


class RetryableOutboundJobError(Exception):
    pass


class StaleOutboundJobError(Exception):
    pass


class OutboundQueueWorker:
    def __init__(
        self,
        session: AsyncSession,
        *,
        connector: CRMChatConnector | None = None,
        settings: Settings | None = None,
        lease_owner: str = "outbound-worker",
        lease_seconds: int = 300,
        allow_real_send: bool | None = None,
        typing_delay_seconds: float | None = None,
        account_id: str | None = None,
    ) -> None:
        self.session = session
        self.repository = OutboundJobRepository(session)
        self.settings = settings or get_settings()
        self.connector = connector or CRMChatConnector(settings=self.settings)
        self._owns_connector = connector is None
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds
        # When set, only claim/send jobs belonging to this account (per-account
        # process with its own CRMchat key). None = claim across all accounts.
        self.account_id = account_id
        self.allow_real_send = (
            self.settings.outbound_real_send_enabled
            if allow_real_send is None
            else allow_real_send
        )
        self.typing_delay_seconds = (
            self.settings.outbound_typing_delay_seconds
            if typing_delay_seconds is None
            else typing_delay_seconds
        )

    async def aclose(self) -> None:
        if self._owns_connector:
            await self.connector.aclose()

    async def process_queued_batch(self, *, limit: int = 50) -> OutboundQueueBatchResult:
        jobs = await self.repository.claim_ready_batch(
            lease_owner=self.lease_owner,
            limit=limit,
            lease_seconds=self.lease_seconds,
            account_id=self.account_id,
        )
        sent = blocked = retry = failed = dead_letter = rescheduled = cancelled = 0

        for job in jobs:
            if job.lease_owner != self.lease_owner:
                continue
            try:
                account = await self.session.get(Account, job.account_id)
                if account is None:
                    raise RuntimeError(f"Account not found: {job.account_id}")
                if self._account_not_ready(account):
                    self._reschedule_for_account(job, account)
                    rescheduled += 1
                    continue

                await self._assert_send_allowed(job)
                job.status = "processing"
                job.attempt_count = (job.attempt_count or 0) + 1
                await self.session.flush()
                await self._send_job(job, account)
            except OutboundSendBlockedError as exc:
                await self._mark_terminal(job, "blocked", exc)
                blocked += 1
            except StaleOutboundJobError as exc:
                await self._mark_terminal(job, "cancelled", exc)
                cancelled += 1
            except TelegramFloodWaitError as exc:
                self._mark_flood_wait(job, account, exc)
                retry += 1
            except CRMChatAPIError as exc:
                if self._is_permanent_send_error(exc):
                    await self._mark_uncontactable(job, exc)
                    failed += 1
                elif self._can_retry(job):
                    self._mark_retry(job, exc)
                    retry += 1
                else:
                    await self._mark_terminal(job, "dead_letter", exc)
                    dead_letter += 1
            except RetryableOutboundJobError as exc:
                if self._can_retry(job):
                    self._mark_retry(job, exc)
                    retry += 1
                else:
                    await self._mark_terminal(job, "dead_letter", exc)
                    dead_letter += 1
            except httpx.TransportError as exc:
                if self._can_retry(job):
                    self._mark_retry(job, exc)
                    retry += 1
                else:
                    await self._mark_terminal(job, "dead_letter", exc)
                    dead_letter += 1
            except Exception as exc:
                await self._mark_terminal(job, "failed", exc)
                failed += 1
            else:
                sent += 1
            finally:
                job.lease_owner = None
                job.lease_expires_at = None

        await self.session.commit()
        return OutboundQueueBatchResult(
            total=len(jobs),
            sent=sent,
            blocked=blocked,
            retry=retry,
            failed=failed,
            dead_letter=dead_letter,
            rescheduled=rescheduled,
            cancelled=cancelled,
        )

    def _account_not_ready(self, account: Account) -> bool:
        now = datetime.now(UTC)
        return bool(
            (account.flood_wait_until and account.flood_wait_until > now)
            or (account.next_available_at and account.next_available_at > now)
        )

    def _reschedule_for_account(self, job: OutboundJob, account: Account) -> None:
        candidates = [
            value
            for value in (account.flood_wait_until, account.next_available_at)
            if value is not None
        ]
        next_time = max(candidates) if candidates else datetime.now(UTC) + timedelta(seconds=60)
        job.status = "retry"
        job.next_attempt_at = next_time
        job.scheduled_at = max(job.scheduled_at, next_time)

    async def _assert_send_allowed(self, job: OutboundJob) -> None:
        # Аутрич разрешён, только если выполнено хотя бы одно:
        #   1) диалог лида заведён интейком из разрешённого источника (daivinchik) —
        #      разрешает ПРОАКТИВНЫЙ first-touch такому лиду;
        #   2) в диалоге есть входящие от собеседника — значит человек сам написал
        #      нам / уже идёт переписка, и воронка имеет право ОТВЕЧАТЬ;
        #   3) target_username явно в username-allowlist (ручной тест-аккаунт).
        # Блокируется только холодная проактивная рассылка в молчащие диалоги
        # (ни интейка, ни входящих) — ровно тот баг, ради которого гейт и нужен.
        if (
            not await self._dialog_from_allowed_source(job)
            and not await self._dialog_has_inbound(job)
            and not self._username_allowlisted(job)
        ):
            raise OutboundSendBlockedError(
                f"Cold proactive send blocked (no allowed-source intake, no inbound, "
                f"not allowlisted): {job.target_username or job.dialog_id}"
            )
        if not self.allow_real_send:
            raise OutboundSendBlockedError("Real outbound send is disabled")

    async def _dialog_has_inbound(self, job: OutboundJob) -> bool:
        """True if the candidate has sent at least one inbound message in this
        dialog — i.e. they wrote to us / a real conversation is underway, so the
        funnel is allowed to reply (covers inbound-first leads like 'привет')."""
        if job.dialog_id is None:
            return False
        result = await self.session.execute(
            select(Message.id)
            .where(Message.dialog_id == job.dialog_id, Message.direction == "inbound")
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def _dialog_from_allowed_source(self, job: OutboundJob) -> bool:
        sources = [
            item.strip()
            for item in self.settings.outbound_allowed_intake_sources.split(",")
            if item.strip()
        ]
        if not sources:
            return False
        dialog = await self.session.get(Dialog, job.dialog_id)
        # 1) Synthetic intake dialog (crmchat_dialog_id == "intake:<source>:...").
        if dialog is not None and dialog.crmchat_dialog_id and any(
            dialog.crmchat_dialog_id.startswith(f"intake:{source}:") for source in sources
        ):
            return True
        # 2) Real telegram: dialog whose candidate was captured by an allowed-source
        #    intake (same @username). The funnel runs on the real telegram dialog, so
        #    its replies to a Дайвинчик lead must pass even though its id is telegram:*.
        username = normalize_username(
            (dialog.telegram_username if dialog is not None else None) or job.target_username or ""
        )
        if not username or username == "@":
            return False
        result = await self.session.execute(
            select(LeadIntakeEvent.id)
            .where(
                LeadIntakeEvent.source.in_(sources),
                func.lower(LeadIntakeEvent.telegram_username) == username.lower(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    def _username_allowlisted(self, job: OutboundJob) -> bool:
        raw_items = [
            item.strip()
            for item in self.settings.outbound_allowed_usernames.split(",")
            if item.strip()
        ]
        if not raw_items:
            return False
        username = normalize_username(job.target_username or "")
        if not username or username == "@":
            return False
        allowed = {normalize_username(item) for item in raw_items}
        return username in allowed

    async def _send_job(self, job: OutboundJob, account: Account) -> None:
        if not account.crmchat_workspace_id:
            raise RetryableOutboundJobError("Account has no CRMchat workspace id")
        if job.job_type != "voice" and not _is_scheduled_followup(job) and self.settings.brain_cancel_outbound_on_inbound and await self._has_newer_candidate_activity(job):
            raise StaleOutboundJobError("New candidate activity arrived before outbound send")
        peer = job.peer or await self._resolve_peer(job, account)
        random_id = str(job.telegram_random_id or random.getrandbits(63))
        job.telegram_random_id = random_id
        await self._simulate_typing(job, account, peer)
        if job.job_type != "voice" and not _is_scheduled_followup(job) and self.settings.brain_cancel_outbound_on_inbound and await self._has_newer_candidate_activity(job):
            raise StaleOutboundJobError("New candidate activity arrived during outbound typing delay")
        if job.job_type == "voice":
            result = await self._send_voice_job(job, account, peer, random_id)
        else:
            result = await self.connector.send_message(
                account.crmchat_workspace_id,
                account.crmchat_account_id,
                peer,
                job.text,
                random_id,
            )
        now = datetime.now(UTC)
        job.status = "sent"
        job.sent_at = now
        job.error_message = None
        job.last_error_type = None
        if job.message_id:
            message = await self.session.get(Message, job.message_id)
            if message is not None:
                message.status = "sent"
        account.next_available_at = now + timedelta(
            seconds=account.send_interval_seconds + random.randint(0, account.send_jitter_seconds)
        )
        account.last_error_message = None
        await self._record_sent_log(
            job=job,
            account=account,
            sent_at=now,
        )
        if self.settings.outbound_mark_read_on_send:
            try:
                await self._mark_latest_inbound_read(job, account, peer)
            except Exception as exc:
                logger.warning(
                    "failed to mark inbound as read after send",
                    extra={"job_id": str(job.id), "error": str(exc)},
                )
        await CampaignSequenceService(self.session).mark_outbound_sent(job)
        logger.info(
            "outbound job sent",
            extra={"job_id": str(job.id), "result_type": result.get("_", "unknown")},
        )

    async def _record_sent_log(self, *, job: OutboundJob, account: Account, sent_at: datetime) -> None:
        existing = None
        if job.telegram_random_id:
            result = await self.session.execute(
                select(OutboundSendLog).where(OutboundSendLog.telegram_random_id == job.telegram_random_id).limit(1)
            )
            existing = result.scalar_one_or_none()
        if existing is not None:
            existing.dialog_id = job.dialog_id
            existing.message_id = job.message_id
            existing.attempt_number = job.attempt_count
            existing.status = "sent"
            existing.rate_limit_bucket = f"account:{account.crmchat_account_id}"
            existing.scheduled_at = job.scheduled_at
            existing.sent_at = sent_at
            existing.error_message = None
            return
        self.session.add(
            OutboundSendLog(
                dialog_id=job.dialog_id,
                message_id=job.message_id,
                attempt_number=job.attempt_count,
                status="sent",
                rate_limit_bucket=f"account:{account.crmchat_account_id}",
                telegram_random_id=job.telegram_random_id,
                scheduled_at=job.scheduled_at,
                sent_at=sent_at,
                error_message=None,
            )
        )

    async def _simulate_typing(
        self, job: OutboundJob, account: Account, peer: dict[str, Any]
    ) -> None:
        metadata = job.media_metadata or {}
        if job.job_type == "voice":
            action = job.typing_action or "sendMessageRecordAudioAction"
            delay = metadata.get("recording_delay_seconds", self.settings.outbound_voice_recording_delay_seconds)
        else:
            action = job.typing_action or "sendMessageTypingAction"
            delay = metadata.get("typing_delay_seconds", self._text_typing_delay_seconds(metadata))
        delay = max(0.0, float(delay or 0.0))
        if delay <= 0 or not account.crmchat_workspace_id:
            return
        await self._send_activity_until_send(account, peer, action=action, delay=delay)

    def _text_typing_delay_seconds(self, metadata: dict[str, Any] | None = None) -> float:
        metadata = metadata or {}
        max_delay = max(0.0, float(metadata.get("typing_delay_max_seconds", self.typing_delay_seconds) or 0.0))
        min_delay = max(
            0.0,
            float(metadata.get("typing_delay_min_seconds", self.settings.outbound_typing_min_delay_seconds) or 0.0),
        )
        if max_delay <= 0:
            return 0.0
        if max_delay <= min_delay:
            return max_delay
        return random.uniform(min_delay, max_delay)

    async def _send_activity_until_send(
        self,
        account: Account,
        peer: dict[str, Any],
        *,
        action: str,
        delay: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        refresh_interval_seconds = 4.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await self._set_outbound_activity(account, peer, action=action)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(refresh_interval_seconds, remaining))

    async def _set_outbound_activity(
        self,
        account: Account,
        peer: dict[str, Any],
        *,
        action: str,
    ) -> None:
        try:
            await self.connector.set_typing(
                account.crmchat_workspace_id,
                account.crmchat_account_id,
                peer,
                action=action,
            )
        except Exception as exc:
            logger.warning(
                "failed to set outbound activity before send",
                extra={"action": action, "error": str(exc)},
            )

    async def _send_voice_job(
        self,
        job: OutboundJob,
        account: Account,
        peer: dict[str, Any],
        random_id: str,
    ) -> dict[str, Any]:
        if not job.media_path:
            raise RetryableOutboundJobError("Voice outbound job has no media_path")
        metadata = job.media_metadata or {}
        media_path = resolve_local_media_path(job.media_path)
        return dict(
            await self.connector.send_voice_note(
                account.crmchat_workspace_id,
                account.crmchat_account_id,
                peer,
                media_path,
                random_id,
                caption=str(metadata.get("caption") or ""),
                duration_seconds=int(metadata.get("duration_seconds") or 0),
            )
        )

    async def _resolve_peer(self, job: OutboundJob, account: Account) -> dict[str, Any]:
        if not job.target_username:
            dialog = await self.session.get(Dialog, job.dialog_id)
            if dialog is None or not dialog.telegram_username:
                raise RetryableOutboundJobError("Outbound job has no target username or peer")
            job.target_username = dialog.telegram_username
        if not account.crmchat_workspace_id:
            raise RetryableOutboundJobError("Account has no CRMchat workspace id")
        resolved = await self.connector.resolve_username(
            account.crmchat_workspace_id,
            account.crmchat_account_id,
            job.target_username,
        )
        peer = dict(build_input_peer_from_resolve_username(resolved))
        job.peer = peer
        return peer

    async def _mark_latest_inbound_read(
        self, job: OutboundJob, account: Account, peer: dict[str, Any]
    ) -> None:
        if not account.crmchat_workspace_id:
            return
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == job.dialog_id, Message.direction == "inbound")
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        message = result.scalar_one_or_none()
        if message is None:
            return
        telegram_message_id = telegram_id_from_crmchat_message_id(
            message.crmchat_message_id
        )
        if telegram_message_id is None:
            return
        await self.connector.read_history(
            account.crmchat_workspace_id,
            account.crmchat_account_id,
            peer,
            max_id=telegram_message_id,
        )

    def _mark_flood_wait(
        self, job: OutboundJob, account: Account | None, exc: TelegramFloodWaitError
    ) -> None:
        now = datetime.now(UTC)
        retry_at = now + timedelta(seconds=exc.retry_after_seconds)
        if account is not None:
            account.flood_wait_until = retry_at
            account.health_status = "rate_limited"
            account.last_error_message = str(exc)
        job.status = "retry" if self._can_retry(job) else "dead_letter"
        job.next_attempt_at = retry_at if job.status == "retry" else None
        job.error_message = str(exc)
        job.last_error_type = type(exc).__name__

    def _mark_retry(self, job: OutboundJob, exc: Exception) -> None:
        job.status = "retry"
        job.next_attempt_at = datetime.now(UTC) + timedelta(
            seconds=self._compute_backoff_seconds(job.attempt_count)
        )
        job.error_message = str(exc)
        job.last_error_type = type(exc).__name__

    async def _mark_terminal(self, job: OutboundJob, status: str, exc: Exception) -> None:
        job.status = status
        job.next_attempt_at = None
        job.error_message = str(exc)
        job.last_error_type = type(exc).__name__
        if job.message_id:
            existing = None
            if job.telegram_random_id:
                result = await self.session.execute(
                    select(OutboundSendLog)
                    .where(OutboundSendLog.telegram_random_id == job.telegram_random_id)
                    .limit(1)
                )
                existing = result.scalar_one_or_none()
            if existing is not None:
                existing.dialog_id = job.dialog_id
                existing.message_id = job.message_id
                existing.attempt_number = max(1, job.attempt_count or 0)
                existing.status = status
                existing.scheduled_at = job.scheduled_at
                existing.error_message = str(exc)
                return
            self.session.add(
                OutboundSendLog(
                    dialog_id=job.dialog_id,
                    message_id=job.message_id,
                    attempt_number=max(1, job.attempt_count or 0),
                    status=status,
                    telegram_random_id=job.telegram_random_id,
                    scheduled_at=job.scheduled_at,
                    error_message=str(exc),
                )
            )

    def _can_retry(self, job: OutboundJob) -> bool:
        return (job.attempt_count or 0) < (job.max_attempts or 5)

    @staticmethod
    def _is_permanent_send_error(exc: Exception) -> bool:
        message = str(exc).upper()
        return any(marker in message for marker in _PERMANENT_SEND_ERROR_MARKERS)

    async def _mark_uncontactable(self, job: OutboundJob, exc: Exception) -> None:
        """Permanent Telegram-side rejection (recipient privacy / blocked / gone):
        fail the job without retrying and flag the dialog so it stops being worked."""
        await self._mark_terminal(job, "failed", exc)
        if job.dialog_id is not None:
            dialog = await self.session.get(Dialog, job.dialog_id)
            if dialog is not None:
                dialog.status = "uncontactable"
        logger.info(
            "outbound job permanently undeliverable; flagged uncontactable",
            extra={"job_id": str(job.id), "error": str(exc)},
        )

    def _compute_backoff_seconds(self, attempt_count: int) -> int:
        base_seconds = min(900, 10 * (2 ** max(0, attempt_count - 1)))
        return max(1, int(base_seconds * random.uniform(0.8, 1.2)))

    async def _has_newer_candidate_activity(self, job: OutboundJob) -> bool:
        threshold = job.created_at or job.scheduled_at
        metadata = job.media_metadata or {}
        reply_to_message_id = parse_uuid(metadata.get("reply_to_message_id"))
        ignored_message_filter = [Message.id != reply_to_message_id] if reply_to_message_id is not None else []
        result = await self.session.execute(
            select(Message)
            .where(
                Message.dialog_id == job.dialog_id,
                Message.direction == "inbound",
                *ignored_message_filter,
                func.coalesce(Message.sent_at, Message.created_at) > threshold,
            )
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return True
        state_result = await self.session.execute(
            select(LeadBrainState)
            .where(
                LeadBrainState.dialog_id == job.dialog_id,
                or_(
                    LeadBrainState.last_candidate_activity_at > threshold,
                    LeadBrainState.last_candidate_typing_at > threshold,
                ),
            )
            .limit(1)
        )
        return state_result.scalar_one_or_none() is not None


def _is_scheduled_followup(job: OutboundJob) -> bool:
    """Отложенное сообщение к 18-летию: его нельзя гасить как «устаревшее» при
    новой активности кандидатки — оно намеренно ждёт своей даты."""
    return bool((job.media_metadata or {}).get("scheduled_followup"))


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


def parse_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def telegram_id_from_crmchat_message_id(value: str | None) -> int | None:
    if not value:
        return None
    candidate = value.rsplit(":", 1)[-1]
    if not candidate.isdigit():
        return None
    return int(candidate)


def resolve_local_media_path(value: str) -> Path:
    root = Path.cwd().resolve()
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RetryableOutboundJobError(f"Media path is outside workspace: {value}") from exc
    if not resolved.exists() or not resolved.is_file():
        raise RetryableOutboundJobError(f"Media path does not exist: {value}")
    return resolved
