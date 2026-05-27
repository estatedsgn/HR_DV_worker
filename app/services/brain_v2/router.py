from __future__ import annotations

import re
from typing import Any

from app.services.brain_v2.schemas import BrainMessage, RouterResult


QUESTION_MARKERS = ("?", "что", "как", "почему", "зачем", "сколько", "где", "когда", "why", "what", "how")
REFUSAL_MARKERS = (
    "не интересно",
    "не надо",
    "не хочу",
    "отстань",
    "не пиши",
    "стоп",
    "stop",
    "удали",
    "блок",
)
POSITIVE_MARKERS = ("да", "давай", "интересно", "расскажи", "хочу", "можно", "ок", "го", "подходит")
SUSPICION_MARKERS = ("скам", "обман", "развод", "безопас", "подозр", "не верю", "что за")


class RouterExtractor:
    """Cheap deterministic router/extractor with LLM-compatible output shape."""

    async def extract(
        self,
        *,
        incoming_message: BrainMessage,
        recent_messages: list[BrainMessage],
        state_snapshot: dict[str, Any],
    ) -> RouterResult:
        text = incoming_message.body.strip()
        normalized = normalize(text)
        is_question = normalized.endswith("?") or any(marker in normalized for marker in QUESTION_MARKERS)
        slot_patch = extract_slots(normalized, text)
        question_topics = detect_question_topics(normalized)
        objection_topics = detect_objection_topics(normalized)
        intent_question_topics = question_topics if is_question else []
        primary_intent = primary_intent_for(normalized, intent_question_topics, objection_topics)
        dialogue_act = dialogue_act_for(normalized, intent_question_topics, objection_topics)
        retrieval_topics = sorted(set(question_topics + objection_topics + topics_from_slots(slot_patch)))
        if not retrieval_topics and state_snapshot.get("stage"):
            retrieval_topics = [str(state_snapshot["stage"])]
        return RouterResult(
            primary_intent=primary_intent,
            secondary_intents=secondary_intents_for(normalized, slot_patch),
            dialogue_act=dialogue_act,
            has_candidate_question=is_question and bool(question_topics or normalized.endswith("?")),
            question_topics=question_topics,
            objection_topics=objection_topics,
            slot_patch=slot_patch,
            retrieval_topics=retrieval_topics,
            confidence=confidence_for(primary_intent, slot_patch, question_topics, objection_topics),
        )


def normalize(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def primary_intent_for(text: str, question_topics: list[str], objection_topics: list[str]) -> str:
    if any(marker in text for marker in REFUSAL_MARKERS):
        return "not_interested"
    if "legal" in question_topics or "legal" in objection_topics:
        return "legal_risk"
    if objection_topics:
        return "objection"
    if question_topics:
        return "question"
    if any(marker == text or marker in text for marker in POSITIVE_MARKERS):
        return "interested"
    return "unclear"


def dialogue_act_for(text: str, question_topics: list[str], objection_topics: list[str]) -> str:
    if any(marker in text for marker in REFUSAL_MARKERS):
        return "refusal"
    if objection_topics:
        return "objection"
    if question_topics or text.endswith("?"):
        return "question"
    return "answer"


def detect_question_topics(text: str) -> list[str]:
    topics: list[str] = []
    if any(marker in text for marker in ["откуда", "где нашла", "почему я", "why me"]):
        topics.append("trust.contact_source")
    if any(marker in text for marker in ["нюд", "гол", "18+", "интим", "nude"]):
        topics.append("trust.no_nudity")
    if any(marker in text for marker in ["англ", "english", "язык"]):
        topics.append("qualification.english")
    if any(marker in text for marker in ["оборуд", "ноут", "телефон", "iphone", "айфон", "интернет"]):
        topics.append("qualification.equipment")
    if any(marker in text for marker in ["график", "время", "распис", "schedule"]):
        topics.append("qualification.schedule")
    if any(marker in text for marker in ["оплат", "деньг", "зарплат", "payment", "pay"]):
        topics.append("pay")
    if any(marker in text for marker in ["закон", "легал", "договор", "налог", "legal"]):
        topics.append("legal")
    return topics


def detect_objection_topics(text: str) -> list[str]:
    topics: list[str] = []
    if any(marker in text for marker in SUSPICION_MARKERS):
        topics.append("trust.answer_basic_suspicion")
    if "не буду" in text or "не хочу" in text:
        topics.append("refusal")
    return topics


def secondary_intents_for(text: str, slot_patch: dict[str, Any]) -> list[str]:
    intents: list[str] = []
    if slot_patch:
        intents.append("slot_update")
    if re.search(r"\b(потом|позже|завтра|later)\b", text):
        intents.append("delay_request")
    return intents


def extract_slots(text: str, original: str) -> dict[str, Any]:
    slots: dict[str, Any] = {}
    age = extract_age(text)
    if age is not None:
        slots["age"] = age
        slots["age_confirmed_18"] = age >= 18
    phone = extract_phone(original)
    if phone:
        slots["phone"] = phone
    if any(marker in text for marker in ["английский норм", "english ok", "b1", "b2", "c1", "c2", "разговор"]):
        slots["english_level"] = "ok"
    elif "англий" in text or "english" in text:
        slots["english_level"] = "basic_or_unclear"
    if any(marker in text for marker in ["ноут", "комп", "айфон", "iphone", "телефон", "интернет"]):
        slots["equipment_status"] = "mentioned"
    if re.search(r"\b([1-9]|1[0-2])\s*(час|ч|hours?)\b", text):
        slots["availability"] = text
    if any(marker in text for marker in ["без нюд", "без гол", "подходит", "ок"]) and any(
        marker in text for marker in ["нюд", "гол", "формат", "границ"]
    ):
        slots["boundaries_ok"] = True
    if any(marker in text for marker in ["зовут", "меня", "я "]) and len(original.split()) <= 4:
        name = extract_name(original)
        if name:
            slots["name"] = name
    if any(marker in text for marker in ["хобби", "люблю", "увлекаюсь"]):
        slots["hobbies"] = original.strip()
    if any(marker in text for marker in ["интервью", "созвон", "готов", "соглас"]):
        slots["interview_interest"] = True
    if any(marker in text for marker in ["утром", "днем", "вечером", "завтра", "сегодня"]):
        slots["preferred_time"] = original.strip()
    return slots


def extract_age(text: str) -> int | None:
    for match in re.finditer(r"\b(\d{1,2})\b", text):
        value = int(match.group(1))
        if 12 <= value <= 80:
            return value
    return None


def extract_phone(text: str) -> str | None:
    cleaned = "".join(ch for ch in text if ch.isdigit() or ch == "+")
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    return cleaned if 10 <= len(digits) <= 15 else None


def extract_name(text: str) -> str | None:
    cleaned = text.strip().strip(".,!?:;")
    lowered = cleaned.lower()
    for prefix in ("меня зовут ", "зовут ", "я "):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return None
    words = cleaned.split()
    if 1 <= len(words) <= 2:
        return " ".join(word.capitalize() for word in words)
    return None


def topics_from_slots(slot_patch: dict[str, Any]) -> list[str]:
    topics = []
    if "age" in slot_patch:
        topics.append("age.confirm_18")
    if "english_level" in slot_patch:
        topics.append("qualification.english")
    if "equipment_status" in slot_patch:
        topics.append("qualification.equipment")
    if "availability" in slot_patch:
        topics.append("qualification.schedule")
    return topics


def confidence_for(
    primary_intent: str,
    slot_patch: dict[str, Any],
    question_topics: list[str],
    objection_topics: list[str],
) -> float:
    if primary_intent in {"not_interested", "legal_risk"}:
        return 0.95
    if slot_patch or question_topics or objection_topics:
        return 0.8
    if primary_intent == "interested":
        return 0.75
    return 0.45
