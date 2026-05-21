from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inbound_event import InboundEvent
from app.repositories.inbound_event import InboundEventRepository
from app.services.campaign_sequence import CampaignSequenceService

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class InboundQueueBatchResult:
    total: int
    processed: int
    failed: int
    retry: int
    dead_letter: int


class RetryableInboundEventError(Exception):
    pass


class InboundQueueWorker:
    def __init__(self, session: AsyncSession, *, lease_owner: str = "worker", lease_seconds: int = 60) -> None:
        self.session = session
        self.repository = InboundEventRepository(session)
        self.lease_owner = lease_owner
        self.lease_seconds = lease_seconds

    async def process_queued_batch(self, *, limit: int = 100) -> InboundQueueBatchResult:
        events = await self.repository.claim_ready_batch(
            lease_owner=self.lease_owner,
            limit=limit,
            lease_seconds=self.lease_seconds,
        )
        logger.info(
            "claimed inbound queue batch",
            extra={
                "lease_owner": self.lease_owner,
                "batch_size": limit,
                "claimed_count": len(events),
            },
        )
        processed = failed = retry = dead_letter = 0

        for event in events:
            if event.lease_owner != self.lease_owner:
                continue
            event.status = "processing"
            event.attempt_count += 1
            await self.session.flush()

            try:
                await self._process_event(event)
            except RetryableInboundEventError as exc:
                event.error_message = str(exc)
                event.last_error_type = type(exc).__name__
                if event.attempt_count >= event.max_attempts:
                    event.status = "dead_letter"
                    event.processed_at = datetime.now(UTC)
                    event.next_attempt_at = None
                    dead_letter += 1
                else:
                    event.status = "retry"
                    event.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=self._compute_backoff_seconds(event.attempt_count)
                    )
                    retry += 1
            except Exception as exc:
                event.status = "failed"
                event.error_message = str(exc)
                event.last_error_type = type(exc).__name__
                event.processed_at = datetime.now(UTC)
                event.next_attempt_at = None
                failed += 1
            else:
                event.status = "processed"
                event.error_message = None
                event.last_error_type = None
                event.processed_at = datetime.now(UTC)
                event.next_attempt_at = None
                processed += 1
            finally:
                event.lease_owner = None
                event.lease_expires_at = None

        await self.session.commit()
        return InboundQueueBatchResult(
            total=len(events),
            processed=processed,
            failed=failed,
            retry=retry,
            dead_letter=dead_letter,
        )

    def _compute_backoff_seconds(self, attempt_count: int) -> int:
        base_seconds = min(300, 5 * (2 ** max(0, attempt_count - 1)))
        jitter_multiplier = random.uniform(0.8, 1.2)
        return max(1, int(base_seconds * jitter_multiplier))

    async def _process_event(self, event: InboundEvent) -> None:
        payload = event.payload or {}
        if payload.get("simulate") == "retry":
            raise RetryableInboundEventError("temporary downstream error")
        if payload.get("simulate") == "fail":
            raise RuntimeError("non-retryable processing error")
        if payload.get("outgoing"):
            return
        dialog_id = getattr(event, "dialog_id", None) or payload.get("dialog_id")
        if dialog_id:
            await CampaignSequenceService(self.session).handle_inbound_message(
                dialog_id=str(dialog_id),
                message_id=payload.get("db_message_id"),
            )
