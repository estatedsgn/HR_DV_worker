from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.account import Account
from app.models.dialog import Dialog
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


@dataclass(slots=True, frozen=True)
class OutboundQueueBatchResult:
    total: int
    sent: int
    blocked: int
    retry: int
    failed: int
    dead_letter: int
    rescheduled: int


class OutboundSendBlockedError(Exception):
    pass


class RetryableOutboundJobError(Exception):
    pass


class OutboundQueueWorker:
    def __init__(
        self,
        session: AsyncSession,
        *,
        connector: CRMChatConnector | None = None,
        settings: Settings | None = None,
        lease_owner: str = "outbound-worker",
        lease_seconds: int = 60,
        allow_real_send: bool | None = None,
    ) -> None:
        self.session = session
        self.repository = OutboundJobRepository(session)
        self.settings = settings or get_settings()
        self.connector = connector or CRMChatConnector(settings=self.settings)
        self._owns_connector = connector is None
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds
        self.allow_real_send = (
            self.settings.outbound_real_send_enabled
            if allow_real_send is None
            else allow_real_send
        )

    async def aclose(self) -> None:
        if self._owns_connector:
            await self.connector.aclose()

    async def process_queued_batch(self, *, limit: int = 50) -> OutboundQueueBatchResult:
        jobs = await self.repository.claim_ready_batch(
            lease_owner=self.lease_owner,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        sent = blocked = retry = failed = dead_letter = rescheduled = 0

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

                self._assert_send_allowed(job)
                job.status = "processing"
                job.attempt_count = (job.attempt_count or 0) + 1
                await self.session.flush()
                await self._send_job(job, account)
            except OutboundSendBlockedError as exc:
                self._mark_terminal(job, "blocked", exc)
                blocked += 1
            except TelegramFloodWaitError as exc:
                self._mark_flood_wait(job, account, exc)
                retry += 1
            except CRMChatAPIError as exc:
                if self._can_retry(job):
                    self._mark_retry(job, exc)
                    retry += 1
                else:
                    self._mark_terminal(job, "dead_letter", exc)
                    dead_letter += 1
            except RetryableOutboundJobError as exc:
                if self._can_retry(job):
                    self._mark_retry(job, exc)
                    retry += 1
                else:
                    self._mark_terminal(job, "dead_letter", exc)
                    dead_letter += 1
            except Exception as exc:
                self._mark_terminal(job, "failed", exc)
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

    def _assert_send_allowed(self, job: OutboundJob) -> None:
        username = normalize_username(job.target_username or "")
        allowed = {normalize_username(item) for item in self.settings.outbound_allowed_usernames.split(",") if item.strip()}
        if username not in allowed:
            raise OutboundSendBlockedError(f"Target username is not allowlisted: {username}")
        if not self.allow_real_send:
            raise OutboundSendBlockedError("Real outbound send is disabled")

    async def _send_job(self, job: OutboundJob, account: Account) -> None:
        if not account.crmchat_workspace_id:
            raise RetryableOutboundJobError("Account has no CRMchat workspace id")
        peer = job.peer or await self._resolve_peer(job, account)
        random_id = str(job.telegram_random_id or random.getrandbits(63))
        job.telegram_random_id = random_id
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
        self.session.add(
            OutboundSendLog(
                dialog_id=job.dialog_id,
                message_id=job.message_id,
                attempt_number=job.attempt_count,
                status="sent",
                rate_limit_bucket=f"account:{account.crmchat_account_id}",
                telegram_random_id=job.telegram_random_id,
                scheduled_at=job.scheduled_at,
                sent_at=now,
                error_message=None,
            )
        )
        await CampaignSequenceService(self.session).mark_outbound_sent(job)
        logger.info(
            "outbound job sent",
            extra={"job_id": str(job.id), "result_type": result.get("_", "unknown")},
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

    def _mark_terminal(self, job: OutboundJob, status: str, exc: Exception) -> None:
        job.status = status
        job.next_attempt_at = None
        job.error_message = str(exc)
        job.last_error_type = type(exc).__name__
        if job.message_id:
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

    def _compute_backoff_seconds(self, attempt_count: int) -> int:
        base_seconds = min(900, 10 * (2 ** max(0, attempt_count - 1)))
        return max(1, int(base_seconds * random.uniform(0.8, 1.2)))


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username
