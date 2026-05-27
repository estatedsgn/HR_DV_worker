from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_snippet import KnowledgeSnippet
from app.repositories.knowledge_snippet import KnowledgeSnippetRepository
from app.services.llm_adapter import LLMAdapter


@dataclass(slots=True, frozen=True)
class KnowledgeSearchResult:
    snippet: KnowledgeSnippet
    score: float


class KnowledgeBaseService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repository = KnowledgeSnippetRepository(session)

    async def import_jsonl(self, path: Path, *, source: str | None = None) -> int:
        created = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            text = str(payload["text"]).strip()
            if not text:
                continue
            self.session.add(
                KnowledgeSnippet(
                    snippet_type=str(payload.get("snippet_type") or "template"),
                    text=text,
                    tags=normalize_tags(payload.get("tags")),
                    source=source or payload.get("source") or path.name,
                    metadata_json=payload.get("metadata_json") or {},
                )
            )
            created += 1
        await self.session.commit()
        return created

    async def search(self, query: str, *, top_k: int = 5) -> list[KnowledgeSearchResult]:
        snippets = await self.repository.list_for_search()
        query_tokens = tokenize(query)
        scored: list[KnowledgeSearchResult] = []
        for snippet in snippets:
            text_score = overlap_score(query_tokens, tokenize(snippet.text))
            tag_score = overlap_score(query_tokens, tokenize(json.dumps(snippet.tags, ensure_ascii=False)))
            score = text_score + tag_score * 0.5
            if score > 0:
                scored.append(KnowledgeSearchResult(snippet=snippet, score=score))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:top_k]

    async def embed_missing(self, *, batch_size: int = 50) -> int:
        snippets = [
            snippet
            for snippet in await self.repository.list_for_search(limit=1000)
            if snippet.embedding is None
        ]
        if not snippets:
            return 0
        updated = 0
        async with LLMAdapter() as adapter:
            for start in range(0, len(snippets), batch_size):
                batch = snippets[start : start + batch_size]
                embeddings = await adapter.embed_texts([snippet.text for snippet in batch])
                for snippet, embedding in zip(batch, embeddings, strict=True):
                    await self.session.execute(
                        sql_text(
                            "UPDATE knowledge_snippets "
                            "SET embedding = CAST(:embedding AS vector), embedding_model = :embedding_model "
                            "WHERE id = :snippet_id"
                        ),
                        {
                            "embedding": vector_literal(embedding),
                            "embedding_model": adapter.settings.llm_embedding_model,
                            "snippet_id": snippet.id,
                        },
                    )
                    updated += 1
        await self.session.commit()
        return updated


def normalize_tags(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"items": value}
    if isinstance(value, str) and value.strip():
        return {"items": [item.strip() for item in value.split(",") if item.strip()]}
    return {}


def tokenize(value: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return {token for token in normalized.split() if len(token) >= 3}


def overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), 1)


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"
