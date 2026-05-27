from __future__ import annotations

from sqlalchemy import select

from app.models.knowledge_snippet import KnowledgeSnippet
from app.repositories.base import BaseRepository


class KnowledgeSnippetRepository(BaseRepository[KnowledgeSnippet]):
    model = KnowledgeSnippet

    async def list_for_search(self, limit: int = 200) -> list[KnowledgeSnippet]:
        result = await self.session.execute(
            select(KnowledgeSnippet).order_by(KnowledgeSnippet.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
