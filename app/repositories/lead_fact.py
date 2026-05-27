from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.lead_fact import LeadFact
from app.repositories.base import BaseRepository


class LeadFactRepository(BaseRepository[LeadFact]):
    model = LeadFact

    async def list_by_lead(self, lead_id) -> list[LeadFact]:
        result = await self.session.execute(
            select(LeadFact)
            .where(LeadFact.lead_id == lead_id)
            .order_by(LeadFact.fact_key.asc())
        )
        return list(result.scalars().all())

    async def upsert_fact(
        self,
        *,
        lead_id,
        dialog_id,
        fact_key: str,
        fact_value: str | None = None,
        fact_value_json: dict[str, Any] | None = None,
        source: str = "llm",
        confidence: float | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> LeadFact:
        result = await self.session.execute(
            select(LeadFact).where(
                LeadFact.lead_id == lead_id,
                LeadFact.fact_key == fact_key,
            )
        )
        fact = result.scalar_one_or_none()
        if fact is None:
            fact = LeadFact(
                lead_id=lead_id,
                dialog_id=dialog_id,
                fact_key=fact_key,
                fact_value=fact_value,
                fact_value_json=fact_value_json,
                source=source,
                confidence=confidence,
                metadata_json=metadata_json or {},
            )
            self.session.add(fact)
        else:
            fact.fact_value = fact_value
            fact.fact_value_json = fact_value_json
            fact.source = source
            fact.confidence = confidence
            fact.metadata_json = metadata_json or fact.metadata_json or {}
        await self.session.flush()
        return fact
