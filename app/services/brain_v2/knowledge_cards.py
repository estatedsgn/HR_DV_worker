from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brain_v2 import KnowledgeCard, RetrievalEvent
from app.services.brain_v2.llm_provider import BrainLLMAdapter
from app.services.brain_v2.schemas import RetrievedKnowledgeCard
from app.services.knowledge_base import tokenize, vector_literal


@dataclass(slots=True, frozen=True)
class RetrievalResult:
    cards: list[RetrievedKnowledgeCard]
    scores: dict[str, float]
    latency_ms: int


class KnowledgeCardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_knowledge_card(
        self,
        *,
        card_key: str,
        content: str,
        stage: str | None = None,
        topic: str | None = None,
        triggers: dict[str, Any] | None = None,
        tags: dict[str, Any] | None = None,
        verification_status: str = "approved",
        metadata_json: dict[str, Any] | None = None,
        active: bool = True,
    ) -> KnowledgeCard:
        card = KnowledgeCard(
            card_key=card_key,
            content=content,
            stage=stage,
            topic=topic,
            triggers=triggers or {},
            tags=tags or {},
            verification_status=verification_status,
            active=active,
            metadata_json=metadata_json or {},
        )
        self.session.add(card)
        await self.session.flush()
        return card

    async def update_knowledge_card(self, card_key: str, **updates: Any) -> KnowledgeCard | None:
        card = await self.get_by_key(card_key)
        if card is None:
            return None
        for key, value in updates.items():
            if hasattr(card, key):
                setattr(card, key, value)
        await self.session.flush()
        return card

    async def embed_knowledge_card(self, card_key: str) -> bool:
        card = await self.get_by_key(card_key)
        if card is None:
            return False
        adapter = BrainLLMAdapter()
        embeddings = await adapter.embed_texts([embedding_text_for_card(card)])
        if not embeddings:
            return False
        embedding_model = f"{adapter.settings.default_embedding_provider}/{adapter.settings.default_embedding_model}"
        await self.session.execute(
            sql_text(
                "UPDATE knowledge_cards "
                "SET embedding = CAST(:embedding AS vector), embedding_model = :embedding_model "
                "WHERE card_key = :card_key"
            ),
            {
                "embedding": vector_literal(embeddings[0]),
                "embedding_model": embedding_model,
                "card_key": card_key,
            },
        )
        await self.session.flush()
        return True

    async def search_knowledge_cards(
        self,
        query: str,
        *,
        stage: str | None = None,
        topics: list[str] | None = None,
        top_k: int = 5,
        verification_statuses: set[str] | None = None,
    ) -> list[RetrievedKnowledgeCard]:
        result = await self.retrieve_for_turn(
            query=query,
            stage=stage,
            topics=topics or [],
            tags=[],
            top_k=top_k,
            verification_statuses=verification_statuses,
        )
        return result.cards

    async def find_cards_by_triggers(
        self,
        *,
        query: str,
        stage: str | None = None,
        topics: list[str] | None = None,
        tags: list[str] | None = None,
        verification_statuses: set[str] | None = None,
    ) -> list[RetrievedKnowledgeCard]:
        cards = await self._candidate_cards(stage=stage, verification_statuses=verification_statuses)
        query_tokens = tokenize(query)
        topic_set = set(topics or [])
        tag_set = set(tags or [])
        results: list[RetrievedKnowledgeCard] = []
        for card in cards:
            score, reasons = trigger_score(card, query_tokens, topic_set, tag_set)
            if score > 0:
                results.append(to_retrieved_card(card, score, reasons))
        results.sort(key=lambda item: item.score, reverse=True)
        return results

    async def retrieve_for_turn(
        self,
        *,
        query: str,
        stage: str | None,
        topics: list[str],
        tags: list[str] | None = None,
        top_k: int = 5,
        verification_statuses: set[str] | None = None,
        query_embedding: list[float] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        trigger_matches = await self.find_cards_by_triggers(
            query=query,
            stage=stage,
            topics=topics,
            tags=tags or [],
            verification_statuses=verification_statuses,
        )
        vector_matches = await self._vector_search(query_embedding, stage=stage, limit=top_k)
        merged = merge_and_rerank(trigger_matches + vector_matches, top_k=top_k)
        scores = {card.card_key: card.score for card in merged}
        return RetrievalResult(
            cards=merged,
            scores=scores,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    async def record_retrieval_event(
        self,
        *,
        brain_run_id=None,
        lead_id=None,
        dialog_id=None,
        stage: str | None,
        query: str | None,
        topics: list[str],
        result: RetrievalResult,
    ) -> None:
        self.session.add(
            RetrievalEvent(
                brain_run_id=brain_run_id,
                lead_id=lead_id,
                dialog_id=dialog_id,
                stage=stage,
                query=query,
                topics={"items": topics},
                retrieved_card_ids=[card.id for card in result.cards],
                scores=result.scores,
                latency_ms=result.latency_ms,
            )
        )
        await self.session.flush()

    async def get_by_key(self, card_key: str) -> KnowledgeCard | None:
        result = await self.session.execute(
            select(KnowledgeCard).where(KnowledgeCard.card_key == card_key).limit(1)
        )
        return result.scalar_one_or_none()

    async def _candidate_cards(
        self,
        *,
        stage: str | None,
        verification_statuses: set[str] | None,
    ) -> list[KnowledgeCard]:
        statuses = verification_statuses or {"approved", "verified"}
        query = select(KnowledgeCard).where(
            KnowledgeCard.active.is_(True),
            KnowledgeCard.verification_status.in_(statuses),
        )
        if stage:
            query = query.where((KnowledgeCard.stage == stage) | (KnowledgeCard.stage.is_(None)))
        result = await self.session.execute(query.order_by(KnowledgeCard.created_at.asc()))
        return list(result.scalars().all())

    async def search_vector_by_embedding(
        self, embedding: list[float], *, stage: str | None = None, limit: int = 5
    ) -> list[RetrievedKnowledgeCard]:
        return await self._vector_search(embedding, stage=stage, limit=limit)

    async def _vector_search(
        self, embedding: list[float] | None, *, stage: str | None, limit: int
    ) -> list[RetrievedKnowledgeCard]:
        if not embedding:
            return []
        filters = "active = true AND verification_status IN ('approved', 'verified') AND embedding IS NOT NULL"
        params: dict[str, Any] = {"embedding": vector_literal(embedding), "limit": limit}
        if stage:
            filters += " AND (stage = :stage OR stage IS NULL)"
            params["stage"] = stage
        result = await self.session.execute(
            sql_text(
                f"""
                SELECT id, card_key, stage, topic, content, 1 - (embedding <=> CAST(:embedding AS vector)) AS score
                FROM knowledge_cards
                WHERE {filters}
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            params,
        )
        return [
            RetrievedKnowledgeCard(
                id=str(row.id),
                card_key=row.card_key,
                stage=row.stage,
                topic=row.topic,
                content=row.content,
                score=float(row.score or 0.0),
                match_reasons=["vector"],
            )
            for row in result.fetchall()
        ]

    createKnowledgeCard = create_knowledge_card
    updateKnowledgeCard = update_knowledge_card
    embedKnowledgeCard = embed_knowledge_card
    searchKnowledgeCards = search_knowledge_cards
    searchVectorByEmbedding = search_vector_by_embedding
    findCardsByTriggers = find_cards_by_triggers
    retrieveForTurn = retrieve_for_turn


def trigger_score(
    card: KnowledgeCard, query_tokens: set[str], topics: set[str], tags: set[str]
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if card.topic and card.topic in topics:
        score += 2.0
        reasons.append("topic")
    if card.stage and card.stage in topics:
        score += 0.5
        reasons.append("stage_topic")
    trigger_tokens = jsonb_tokens(card.triggers)
    tag_tokens = jsonb_tokens(card.tags)
    content_tokens = tokenize(card.content)
    overlap = len(query_tokens & trigger_tokens)
    if overlap:
        score += 1.5 + overlap * 0.2
        reasons.append("trigger")
    tag_overlap = len(tags & tag_tokens) if tags else len(query_tokens & tag_tokens)
    if tag_overlap:
        score += 0.8 + tag_overlap * 0.1
        reasons.append("tag")
    content_overlap = len(query_tokens & content_tokens)
    if content_overlap:
        score += min(1.0, content_overlap * 0.1)
        reasons.append("content")
    if is_mentor_reference(card):
        score += 0.35
        reasons.append("mentor_reference")
    return score, reasons


def jsonb_tokens(value: Any) -> set[str]:
    if isinstance(value, dict):
        raw_items = []
        for item in value.values():
            if isinstance(item, list):
                raw_items.extend(str(v) for v in item)
            else:
                raw_items.append(str(item))
        return tokenize(" ".join(raw_items))
    if isinstance(value, list):
        return tokenize(" ".join(str(item) for item in value))
    return tokenize(str(value or ""))


def is_mentor_reference(card: KnowledgeCard) -> bool:
    metadata = card.metadata_json or {}
    source_bundle = str(metadata.get("source_bundle") or metadata.get("source") or "").lower()
    card_key = str(card.card_key or "").lower()
    tags = jsonb_tokens(card.tags)
    return "mentor" in source_bundle or card_key.startswith("mentor.") or "mentor" in tags


def to_retrieved_card(card: KnowledgeCard, score: float, reasons: list[str]) -> RetrievedKnowledgeCard:
    return RetrievedKnowledgeCard(
        id=str(card.id),
        card_key=card.card_key,
        stage=card.stage,
        topic=card.topic,
        content=card.content,
        score=score,
        match_reasons=reasons,
    )


def embedding_text_for_card(card: KnowledgeCard) -> str:
    metadata = card.metadata_json or {}
    embedding_text = metadata.get("embedding_text")
    if isinstance(embedding_text, str) and embedding_text.strip():
        return embedding_text
    return card.content


def merge_and_rerank(cards: list[RetrievedKnowledgeCard], *, top_k: int) -> list[RetrievedKnowledgeCard]:
    merged: dict[str, RetrievedKnowledgeCard] = {}
    for card in cards:
        current = merged.get(card.card_key)
        if current is None or card.score > current.score:
            merged[card.card_key] = card
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:top_k]
