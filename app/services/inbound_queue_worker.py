from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.inbound_event import InboundEvent
from app.models.lead import Lead
from app.repositories.inbound_event import InboundEventRepository
from app.services.funnel_graph.turn_buffer import FunnelTurnBufferService, is_candidate_typing_event

logger = logging.getLogger(__name__)
LangGraphFunnelGateway = None


@dataclass(slots=True, frozen=True)
class InboundQueueBatchResult:
    total: int
    processed: int
    failed: int
    retry: int
    dead_letter: int


class RetryableInboundEventError(Exception):
    pass


class DeferredInboundEvent(Exception):
    def __init__(self, retry_at: datetime, reason: str) -> None:
        super().__init__(reason)
        self.retry_at = retry_at
        self.reason = reason


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
            except DeferredInboundEvent as exc:
                event.status = "retry"
                event.error_message = exc.reason
                event.last_error_type = type(exc).__name__
                event.next_attempt_at = exc.retry_at
                retry += 1
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
            settings = get_settings()
            if settings.langgraph_funnel_enabled:
                dialog = await self.session.get(Dialog, dialog_id)
                lead = await self._lead_for_dialog(dialog_id) if dialog is not None else None
                runtime = await self._runtime_for_lead(lead) if lead is not None else None
                turn_buffer = FunnelTurnBufferService(
                    self.session,
                    debounce_seconds=settings.brain_inbound_debounce_seconds,
                )
                if dialog is not None and is_restart_command(payload.get("text")):
                    gateway_cls = LangGraphFunnelGateway
                    if gateway_cls is None:
                        from app.services.funnel_graph.gateway import LangGraphFunnelGateway as gateway_cls

                    await gateway_cls(self.session, settings=settings).restart_for_dialog(
                        dialog_id=str(dialog_id),
                        triggering_message_id=payload.get("db_message_id"),
                        current_event_id=str(event.id),
                    )
                    return
                if dialog is not None and is_candidate_typing_event(payload):
                    await turn_buffer.record_typing_activity(dialog=dialog, runtime=runtime, payload=payload)
                    return
                if dialog is not None:
                    buffer_result = await turn_buffer.prepare_inbound_turn(
                        dialog=dialog,
                        lead=lead,
                        runtime=runtime,
                        message_id=payload.get("db_message_id"),
                    )
                    if not buffer_result.ready:
                        if buffer_result.retry_at is not None:
                            raise DeferredInboundEvent(
                                buffer_result.retry_at,
                                buffer_result.reason or "waiting for candidate quiet window",
                            )
                        return
                try:
                    gateway_cls = LangGraphFunnelGateway
                    if gateway_cls is None:
                        from app.services.funnel_graph.gateway import LangGraphFunnelGateway as gateway_cls

                    await gateway_cls(self.session, settings=settings).decide_for_dialog_message(
                        dialog_id=str(dialog_id),
                        message_id=payload.get("db_message_id"),
                    )
                except Exception:
                    logger.exception("langgraph funnel gateway failed", extra={"dialog_id": str(dialog_id)})
                    raise
                return

    async def _lead_for_dialog(self, dialog_id) -> Lead | None:
        from sqlalchemy import select

        result = await self.session.execute(select(Lead).where(Lead.dialog_id == dialog_id).limit(1))
        return result.scalar_one_or_none()

    async def _runtime_for_lead(self, lead: Lead) -> LeadFunnelRuntime | None:
        from sqlalchemy import select

        result = await self.session.execute(
            select(LeadFunnelRuntime).where(LeadFunnelRuntime.lead_id == lead.id).limit(1)
        )
        return result.scalar_one_or_none()


def is_restart_command(text: object) -> bool:
    normalized = str(text or "").strip().lower()
    return normalized in {"restart", "/restart"}
