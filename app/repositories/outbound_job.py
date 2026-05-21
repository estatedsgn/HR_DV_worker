from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import bindparam, select, text

from app.models.outbound_job import OutboundJob
from app.repositories.base import BaseRepository


class OutboundJobRepository(BaseRepository[OutboundJob]):
    model = OutboundJob

    async def claim_ready_batch(
        self, *, lease_owner: str, limit: int = 50, lease_seconds: int = 60
    ) -> list[OutboundJob]:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        query = text(
            """
            WITH candidates AS (
                SELECT id
                FROM outbound_jobs
                WHERE
                    (
                        status = 'queued'
                        OR (status = 'retry' AND next_attempt_at IS NOT NULL AND next_attempt_at <= :now)
                    )
                    AND scheduled_at <= :now
                    AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
                ORDER BY scheduled_at ASC, created_at ASC
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE outbound_jobs AS oj
            SET lease_owner = :lease_owner,
                lease_expires_at = :lease_expires_at
            FROM candidates
            WHERE oj.id = candidates.id
            RETURNING oj.id
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
            select(OutboundJob)
            .where(OutboundJob.id.in_(ids))
            .order_by(OutboundJob.scheduled_at.asc(), OutboundJob.created_at.asc())
        )
        return list(claimed.scalars().all())

    async def get_by_sequence_step(self, *, sequence_run_id, campaign_step_id) -> OutboundJob | None:
        result = await self.session.execute(
            select(OutboundJob)
            .where(
                OutboundJob.sequence_run_id == sequence_run_id,
                OutboundJob.campaign_step_id == campaign_step_id,
            )
            .order_by(OutboundJob.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()
