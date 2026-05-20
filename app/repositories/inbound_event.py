from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, bindparam, or_, select, text

from app.models.inbound_event import InboundEvent
from app.repositories.base import BaseRepository


class InboundEventRepository(BaseRepository[InboundEvent]):
    model = InboundEvent

    async def get_duplicate(
        self, *, external_event_id: str | None, external_message_id: str | None
    ) -> InboundEvent | None:
        filters = []
        if external_event_id:
            filters.append(InboundEvent.external_event_id == external_event_id)
        if external_message_id:
            filters.append(InboundEvent.external_message_id == external_message_id)
        if not filters:
            return None
        result = await self.session.execute(
            select(InboundEvent).where(or_(*filters)).limit(1)
        )
        return result.scalar_one_or_none()

    async def claim_ready_batch(
        self, *, lease_owner: str, limit: int = 100, lease_seconds: int = 60
    ) -> list[InboundEvent]:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        query = text(
            """
            WITH candidates AS (
                SELECT id
                FROM inbound_events
                WHERE
                    (status = 'queued' OR (status = 'retry' AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now))
                    AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
                ORDER BY created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE inbound_events AS ie
            SET lease_owner = :lease_owner,
                lease_expires_at = :lease_expires_at
            FROM candidates
            WHERE ie.id = candidates.id
            RETURNING ie.id
            """
        ).bindparams(
            bindparam("now"),
            bindparam("limit"),
            bindparam("lease_owner"),
            bindparam("lease_expires_at"),
        )
        result = await self.session.execute(
            query,
            {
                "now": now,
                "limit": limit,
                "lease_owner": lease_owner,
                "lease_expires_at": lease_expires_at,
            },
        )
        ids = [row[0] for row in result.fetchall()]
        if not ids:
            return []
        claimed = await self.session.execute(
            select(InboundEvent).where(InboundEvent.id.in_(ids)).order_by(InboundEvent.created_at.asc())
        )
        return list(claimed.scalars().all())
