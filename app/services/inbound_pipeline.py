from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inbound_event import InboundEvent
from app.repositories.inbound_event import InboundEventRepository


class InboundPipelineService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = InboundEventRepository(session)

    async def enqueue(
        self,
        *,
        source: str,
        payload: dict[str, Any],
        external_event_id: str | None = None,
        external_message_id: str | None = None,
        account_id: UUID | None = None,
        dialog_id: UUID | None = None,
    ) -> tuple[InboundEvent, bool]:
        # Two-layer dedupe:
        # 1) pre-check via SELECT to short-circuit common duplicates,
        # 2) DB unique constraints as atomic protection under races.
        duplicate = await self.repository.get_duplicate(
            external_event_id=external_event_id,
            external_message_id=external_message_id,
        )
        if duplicate:
            return duplicate, False

        now = datetime.now(UTC)
        event = InboundEvent(
            source=source,
            payload=payload,
            external_event_id=external_event_id,
            external_message_id=external_message_id,
            account_id=account_id,
            dialog_id=dialog_id,
            status="queued",
            queued_at=now,
            next_attempt_at=now,
        )
        try:
            await self.repository.add(event)
            return event, True
        except IntegrityError:
            await self.session.rollback()
            existing = await self.repository.get_duplicate(
                external_event_id=external_event_id,
                external_message_id=external_message_id,
            )
            if existing:
                return existing, False
            raise
