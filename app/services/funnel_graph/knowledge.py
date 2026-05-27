from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brain_v2.knowledge_cards import KnowledgeCardService
from app.services.funnel_graph.state import RetrievedAnswer


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"


class FunnelKnowledgeAdapter:
    """Adapter that exposes existing knowledge_cards to the LangGraph funnel."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.cards = KnowledgeCardService(session)

    async def retrieve_answers(
        self,
        *,
        query: str,
        stage: str | None,
        topics: list[str] | None = None,
        top_k: int = 5,
    ) -> list[RetrievedAnswer]:
        if not query.strip():
            return []
        result = await self.cards.retrieve_for_turn(
            query=query,
            stage=stage,
            topics=topics or [],
            tags=[],
            top_k=top_k,
        )
        cards = answerable_cards(result.cards)
        if not cards and stage:
            result = await self.cards.retrieve_for_turn(
                query=query,
                stage=None,
                topics=topics or [],
                tags=[],
                top_k=top_k,
            )
            cards = answerable_cards(result.cards)
        return [RetrievedAnswer.model_validate(card.model_dump()) for card in cards]


class StaticFunnelKnowledgeBase:
    """File-backed FAQ/objection/template store for the terminal funnel."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or DEFAULT_KNOWLEDGE_DIR
        self.faq = load_json_list(self.base_dir / "faq.json")
        self.objections = load_json_list(self.base_dir / "objections.json")
        self.templates = load_json_dict(self.base_dir / "templates.json")
        self.voice_packs = load_json_dict(self.base_dir / "voice_packs.json")

    def retrieve(
        self,
        *,
        incoming_message: str,
        current_stage: str,
        recent_messages: list[dict[str, Any]] | None = None,
        retrieval_query: str | None = None,
        retrieval_topics: list[str] | None = None,
    ) -> dict[str, Any]:
        text = repair_mojibake(retrieval_query or incoming_message).strip().lower()
        faq_context = match_knowledge_items(text, self.faq, topics=retrieval_topics)
        objection_context = match_knowledge_items(text, self.objections, topics=retrieval_topics)
        return {
            "faq_context": faq_context,
            "objection_context": objection_context,
            "voice_packs": self.voice_packs,
            "templates": self.templates,
            "response_rules": {
                "current_stage": current_stage,
                "interrupts_return_to_current_question": True,
                "later_is_not_lost": True,
                "do_not_contact_disables_reply": True,
            },
            "retrieved_knowledge": {
                "faq_topics": [item.get("topic") for item in faq_context],
                "objection_topics": [item.get("topic") for item in objection_context],
                "recent_message_count": len(recent_messages or []),
                "knowledge_found": bool(faq_context or objection_context),
                "retrieval_query": text,
                "retrieval_topics": retrieval_topics or [],
            },
        }

    def template(self, template_id: str) -> str:
        return str(self.templates.get(template_id) or "")

    def voice_pack(self, voice_pack_id: str) -> list[str]:
        raw = self.voice_packs.get(voice_pack_id) or []
        return [str(item) for item in raw]


def answerable_cards(cards):
    filtered = [card for card in cards if is_answerable_card(card)]
    return filtered or [card for card in cards if not is_weak_mentor_only_match(card)]


def is_answerable_card(card) -> bool:
    card_key = str(getattr(card, "card_key", "") or "").lower()
    if card_key.startswith(("workflow.", "dialogue_chunk.")):
        return False
    return not is_weak_mentor_only_match(card)


def is_weak_mentor_only_match(card) -> bool:
    reasons = set(getattr(card, "match_reasons", []) or [])
    score = float(getattr(card, "score", 0.0) or 0.0)
    return reasons == {"mentor_reference"} and score <= 0.35


def load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def match_knowledge_items(
    text: str,
    items: list[dict[str, Any]],
    *,
    topics: list[str] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    if not text:
        return []
    topic_set = {str(topic).lower() for topic in topics or [] if topic}
    query_tokens = set(tokenize_for_match(text))
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in items:
        triggers = [str(trigger).lower() for trigger in item.get("triggers") or []]
        score = float(sum(1 for trigger in triggers if trigger and trigger in text))
        topic = str(item.get("topic") or "").lower()
        if topic in topic_set:
            score += 4.0
        if topic and topic in text:
            score += 1.0
        haystack = " ".join([topic, str(item.get("answer") or ""), *triggers]).lower()
        item_tokens = set(tokenize_for_match(haystack))
        overlap = len(query_tokens & item_tokens)
        if overlap:
            score += min(overlap * 0.35, 2.0)
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [dict(item) for _, item in scored[:limit]]


def tokenize_for_match(text: str) -> list[str]:
    stop = {"что", "как", "это", "или", "мне", "тебе", "если", "есть", "про", "для", "где", "кто", "могу"}
    return [token for token in text.replace("ё", "е").split() if len(token) >= 3 and token not in stop]


def repair_mojibake(text: str) -> str:
    if "Р" not in text and "С" not in text:
        return text
    try:
        repaired = text.encode("cp1251").decode("utf-8")
    except UnicodeError:
        return text
    return repaired if any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in repaired) else text
