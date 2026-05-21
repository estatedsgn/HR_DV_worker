from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.dialog_sequence_run import DialogSequenceRun
from app.repositories.base import BaseRepository


class DialogSequenceRunRepository(BaseRepository[DialogSequenceRun]):
    model = DialogSequenceRun

    async def get_active_by_dialog(self, dialog_id) -> DialogSequenceRun | None:
        result = await self.session.execute(
            select(DialogSequenceRun)
            .where(
                DialogSequenceRun.dialog_id == dialog_id,
                DialogSequenceRun.status.in_(["active", "waiting_outbound", "awaiting_reply", "awaiting_llm"]),
            )
            .order_by(DialogSequenceRun.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_stale_waiting_outbound(self, *, older_than_seconds: int = 900) -> list[DialogSequenceRun]:
        threshold = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        result = await self.session.execute(
            select(DialogSequenceRun)
            .where(
                DialogSequenceRun.status == "waiting_outbound",
                DialogSequenceRun.updated_at <= threshold,
            )
            .order_by(DialogSequenceRun.updated_at.asc())
        )
        return list(result.scalars().all())

    async def list_stale_awaiting_llm(self, *, older_than_seconds: int = 900) -> list[DialogSequenceRun]:
        threshold = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        result = await self.session.execute(
            select(DialogSequenceRun)
            .where(
                DialogSequenceRun.status == "awaiting_llm",
                DialogSequenceRun.updated_at <= threshold,
            )
            .order_by(DialogSequenceRun.updated_at.asc())
        )
        return list(result.scalars().all())
