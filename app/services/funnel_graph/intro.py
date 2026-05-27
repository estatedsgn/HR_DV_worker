from __future__ import annotations

import re
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.funnel_graph.state import FunnelGraphState, latest_inbound_text


IntroIntent = Literal["agree", "disagree", "question", "question_and_agree", "unclear"]
LLMIntroClassifierCallable = Callable[[str, FunnelGraphState], Awaitable[Any] | Any]
LLMFAQAnswerCallable = Callable[[str, list[dict[str, Any]], FunnelGraphState], Awaitable[Any] | Any]

INTRO_MESSAGE = (
    "Привет! Увидела твой отклик. Сейчас коротко расскажу условия, "
    "и если тебе будет интересно, дальше задам пару уточняющих вопросов."
)
AGE_QUESTION = "Скажи, пожалуйста, тебе уже есть 18?"
NAME_QUESTION = "как я могу к тебе обращаться?"
AGE_PREFACE_TEMPLATE = (
    "{name} слушай, мне нужно уточнить у тебя точный возраст, ведь у нас работают только совершеннолетние."
)
AGE_EXACT_QUESTION = "скажи, сколько тебе лет?"
GOODBYE_MESSAGE = "Поняла, не буду отвлекать. Хорошего дня!"
FAQ_MANAGER_FALLBACK = "Этот момент лучше уточнит менеджер, чтобы не сказать тебе неточно."
QUESTION_CONTINUE_PROMPT = "Если в целом интересно, могу дальше задать пару уточняющих вопросов."

LLM_INTRO_INTENT_CLASSIFIER_PROMPT = """Ты классификатор первого ответа кандидата в HR-воронке.

Контекст:
Кандидату отправили вступительное сообщение. Теперь он ответил.
Нужно определить смысл его ответа, но НЕ нужно отвечать кандидату.

Твоя задача:
Классифицируй сообщение кандидата в один из intent.

Допустимые intent:

1. "agree"
Кандидат согласен продолжить, проявляет интерес, хочет узнать дальше.
Примеры:
- "да"
- "интересно"
- "расскажи"
- "давай"
- "можно подробнее"
- "го"
- "ок, давай"

2. "disagree"
Кандидат отказывается, ему неинтересно, он просит не писать, отвечает негативно.
Примеры:
- "нет"
- "не интересно"
- "не актуально"
- "не хочу"
- "не пиши"
- "отстань"
- "удали меня"

3. "question"
Кандидат задал вопрос, но явно не согласился продолжать.
Примеры:
- "а что за работа?"
- "сколько платят?"
- "какой график?"
- "что нужно делать?"
- "это удаленно?"
- "а где?"

4. "question_and_agree"
Кандидат одновременно проявил интерес/согласие и задал вопрос.
Примеры:
- "да, интересно, а сколько платят?"
- "расскажи, а какой график?"
- "давай, только что нужно делать?"
- "ок, а это удаленно?"

5. "unclear"
Невозможно уверенно понять смысл сообщения.
Примеры:
- "ну хз"
- "может быть"
- "потом"
- "..."
- сообщение слишком короткое или неоднозначное

Правила:
- Если кандидат просит больше не писать, всегда выбирай "disagree".
- Если есть и интерес, и вопрос, выбирай "question_and_agree".
- Если есть только вопрос без явного согласия, выбирай "question".
- Если сообщение похоже на согласие без вопроса, выбирай "agree".
- Если сообщение похоже на отказ, выбирай "disagree".
- Не придумывай дополнительные intent.
- Не отвечай кандидату.
- Не объясняй вне JSON.
- Верни только валидный JSON.

Формат ответа строго такой:

{
  "intent": "agree | disagree | question | question_and_agree | unclear",
  "confidence": 0.0,
  "reason": "короткое объяснение классификации",
  "detected_question": null,
  "risk_flags": []
}
"""

LLM_FAQ_ANSWER_PROMPT = """Ты HR-ассистент, который отвечает кандидату на вопрос по базе знаний.

Контекст:
Кандидат находится на первом этапе HR-воронки.
Он задал вопрос после вступительного сообщения.
Тебе передан FAQ-контекст из базы знаний.

Твоя задача:
Коротко и естественно ответить на вопрос кандидата, используя только FAQ-контекст.

Правила:
- Отвечай только на основе переданного FAQ-контекста.
- Не выдумывай факты, которых нет в FAQ.
- Не обещай гарантированный доход.
- Не используй давление, манипуляции или агрессивные продажи.
- Не спорь с кандидатом.
- Не пиши длинную простыню.
- Ответ должен быть коротким: 1-4 предложения.
- Если в FAQ нет ответа, честно скажи, что этот момент лучше уточнит менеджер.
- Не задавай вопрос про возраст внутри этого ответа. Возврат к воронке будет добавлен отдельным шагом.
- Не добавляй приветствие, если оно не нужно.
- Не используй Markdown.
- Верни только валидный JSON.

Формат ответа строго такой:

{
  "can_answer_from_faq": true,
  "reply_text": "короткий ответ кандидату",
  "missing_info": null,
  "risk_flags": []
}
"""

AGREE_PATTERNS = (
    "да",
    "давай",
    "интересно",
    "расскажи",
    "слушаю",
    "ок",
    "окей",
    "го",
    "можно",
    "хочу",
)
DISAGREE_PATTERNS = (
    "нет",
    "не интересно",
    "неинтересно",
    "не актуально",
    "не хочу",
    "не пиши",
    "отстань",
    "не надо",
    "удали меня",
    "спасибо не",
)
QUESTION_MARKERS = (
    "?",
    "что",
    "как",
    "сколько",
    "какая",
    "какой",
    "зачем",
    "где",
    "почему",
    "оплата",
    "зарплата",
    "график",
    "работа",
    "удаленно",
    "по времени",
)
OBJECTION_MARKERS = (
    "боюсь",
    "страшно",
    "сомневаюсь",
    "не уверена",
    "не уверен",
    "не умею",
    "не получится",
    "не смогу",
    "нет опыта",
    "без опыта",
    "не знаю английский",
    "плохой английский",
    "не хочу показывать лицо",
    "не хочу светить лицо",
    "нет оборудования",
    "нет комнаты",
    "не подойдет",
    "не подходит",
)


class IntroClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: IntroIntent
    classifier: Literal["cheap", "llm"]
    confidence: float = 0.0
    reason: str = ""
    detected_question: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


class LLMIntroClassificationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: IntroIntent
    confidence: float = 0.0
    reason: str = ""
    detected_question: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


class FAQAnswerResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    can_answer_from_faq: bool = False
    reply_text: str = ""
    missing_info: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


class LLMIntroClassifier:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter = adapter or BrainLLMAdapter(settings=self.settings)

    async def classify(self, text: str, state: FunnelGraphState) -> IntroClassification:
        if not self.adapter.has_api_key("interest_classifier"):
            return IntroClassification(intent="unclear", classifier="llm", reason="missing_api_key")
        try:
            payload = await self.adapter.complete_json(
                component="interest_classifier",
                system_prompt=LLM_INTRO_INTENT_CLASSIFIER_PROMPT,
                user_payload={
                    "stage": "waiting_intro_reply",
                    "last_bot_message": INTRO_MESSAGE,
                    "incoming_message": text,
                },
                response_model=LLMIntroClassificationResult,
            )
        except (BrainLLMError, ValueError, TypeError):
            return IntroClassification(intent="unclear", classifier="llm", reason="llm_error")
        parsed = LLMIntroClassificationResult.model_validate(payload)
        return IntroClassification(classifier="llm", **parsed.model_dump())


class LLMFAQAnswerer:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        adapter: BrainLLMAdapter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.adapter = adapter or BrainLLMAdapter(settings=self.settings)

    async def answer(
        self,
        candidate_question: str,
        faq_context: list[dict[str, Any]],
        state: FunnelGraphState,
    ) -> FAQAnswerResult:
        if not faq_context:
            return FAQAnswerResult(reply_text=FAQ_MANAGER_FALLBACK, missing_info="faq_or_api_key_missing")
        if not self.adapter.has_api_key("dialogue_brain"):
            return answer_from_first_faq_card(faq_context, missing_info="llm_api_key_missing")
        try:
            payload = await self.adapter.complete_json(
                component="dialogue_brain",
                system_prompt=LLM_FAQ_ANSWER_PROMPT,
                user_payload={
                    "candidate_question": candidate_question,
                    "faq_context": faq_context,
                    "stage": "waiting_intro_reply",
                    "intent": state.get("intent") or "question",
                },
                response_model=FAQAnswerResult,
            )
        except (BrainLLMError, ValueError, TypeError):
            return FAQAnswerResult(reply_text=FAQ_MANAGER_FALLBACK, missing_info="llm_error")
        parsed = FAQAnswerResult.model_validate(payload)
        if not parsed.can_answer_from_faq or not parsed.reply_text.strip():
            parsed.reply_text = FAQ_MANAGER_FALLBACK
        return parsed


def cheap_intro_classifier(text: str) -> IntroClassification:
    normalized = normalize_text(text)
    if not normalized:
        return IntroClassification(intent="unclear", classifier="cheap", reason="empty_message")
    has_question = contains_any(normalized, QUESTION_MARKERS)
    has_agree = contains_any(normalized, AGREE_PATTERNS)
    has_disagree = contains_any(normalized, DISAGREE_PATTERNS)
    detected_question = text.strip() if has_question else None
    if has_disagree:
        return IntroClassification(
            intent="disagree",
            classifier="cheap",
            confidence=0.95,
            reason="explicit_disagree",
            detected_question=detected_question,
        )
    if has_question and has_agree:
        return IntroClassification(
            intent="question_and_agree",
            classifier="cheap",
            confidence=0.9,
            reason="question_and_agree_markers",
            detected_question=detected_question,
        )
    if has_question:
        return IntroClassification(
            intent="question",
            classifier="cheap",
            confidence=0.85,
            reason="question_marker",
            detected_question=detected_question,
        )
    if has_agree:
        return IntroClassification(intent="agree", classifier="cheap", confidence=0.85, reason="agree_marker")
    return IntroClassification(intent="unclear", classifier="cheap", reason="no_clear_marker")


def detect_knowledge_need(text: str) -> dict[str, Any]:
    normalized = normalize_text(text)
    if not normalized:
        return {"needs_answer": False, "kind": None, "detected_question": None}
    has_question = contains_any(normalized, QUESTION_MARKERS)
    has_objection = contains_any(normalized, OBJECTION_MARKERS)
    if has_question and has_objection:
        return {"needs_answer": True, "kind": "question_and_objection", "detected_question": text.strip()}
    if has_question:
        return {"needs_answer": True, "kind": "question", "detected_question": text.strip()}
    if has_objection:
        return {"needs_answer": True, "kind": "objection", "detected_question": text.strip()}
    return {"needs_answer": False, "kind": None, "detected_question": None}


async def classify_intro_intent(
    state: FunnelGraphState,
    *,
    llm_classifier: LLMIntroClassifierCallable | None = None,
) -> IntroClassification:
    text = latest_inbound_text(state)
    classification = cheap_intro_classifier(text)
    if classification.intent != "unclear":
        return classification
    llm_classifier = llm_classifier or default_llm_intro_classifier
    result = llm_classifier(text, state)
    if hasattr(result, "__await__"):
        result = await result
    return normalize_classification_result(result)


async def answer_intro_question(
    state: FunnelGraphState,
    *,
    faq_answerer: LLMFAQAnswerCallable | None = None,
) -> FAQAnswerResult:
    question = str(state.get("metadata", {}).get("detected_question") or latest_inbound_text(state))
    faq_context = faq_context_from_cards(list(state.get("retrieved_cards") or []))
    faq_answerer = faq_answerer or default_llm_faq_answerer
    result = faq_answerer(question, faq_context, state)
    if hasattr(result, "__await__"):
        result = await result
    parsed = normalize_faq_answer_result(result)
    if not parsed.can_answer_from_faq or not parsed.reply_text.strip():
        parsed.reply_text = FAQ_MANAGER_FALLBACK
    return parsed


async def default_llm_intro_classifier(text: str, state: FunnelGraphState) -> IntroClassification:
    return IntroClassification(intent="unclear", classifier="llm", reason="no_llm_classifier_configured")


async def default_llm_faq_answerer(
    candidate_question: str,
    faq_context: list[dict[str, Any]],
    state: FunnelGraphState,
) -> FAQAnswerResult:
    if not faq_context:
        return FAQAnswerResult(reply_text=FAQ_MANAGER_FALLBACK, missing_info="empty_faq_context")
    return answer_from_first_faq_card(faq_context, missing_info="empty_faq_answer")


def answer_from_first_faq_card(
    faq_context: list[dict[str, Any]],
    *,
    missing_info: str | None = None,
) -> FAQAnswerResult:
    for item in faq_context:
        answer = clean_card_answer(str(item.get("answer") or ""))
        if answer:
            return FAQAnswerResult(can_answer_from_faq=True, reply_text=answer, missing_info=None)
    return FAQAnswerResult(reply_text=FAQ_MANAGER_FALLBACK, missing_info=missing_info)


def plan_intro_turn(state: FunnelGraphState) -> FunnelGraphState:
    stage = state.get("stage") or "new"
    if should_plan_global_knowledge_answer(state):
        return plan_global_knowledge_turn(state)
    if stage == "new":
        return send_text_patch(
            stage="waiting_intro_reply",
            reply_text=INTRO_MESSAGE,
            idempotency_key=f"{state.get('thread_id')}:intro",
            intent=None,
        )
    if stage == "waiting_name":
        return plan_name_turn(state)
    if stage != "waiting_intro_reply":
        return {"pending_actions": []}

    intent = normalize_intent(state.get("intent") or "unclear")
    if intent == "agree":
        return send_text_patch(
            stage="waiting_name",
            reply_text=NAME_QUESTION,
            idempotency_key=f"{state.get('thread_id')}:name-question",
            intent=intent,
        )
    if intent == "disagree":
        return send_text_patch(
            stage="lost",
            status="closed",
            reply_text=GOODBYE_MESSAGE,
            idempotency_key=f"{state.get('thread_id')}:goodbye",
            intent=intent,
        )
    if intent in {"question", "question_and_agree"}:
        answer = str(state.get("metadata", {}).get("faq_reply_text") or FAQ_MANAGER_FALLBACK)
        if intent == "question_and_agree":
            reply_text = f"{answer}\n\n{NAME_QUESTION}"
            return send_text_patch(
                stage="waiting_name",
                reply_text=reply_text,
                idempotency_key=f"{state.get('thread_id')}:intro-answer-name",
                intent=intent,
            )
        reply_text = f"{answer}\n\n{QUESTION_CONTINUE_PROMPT}"
        return send_text_patch(
            stage="waiting_intro_reply",
            reply_text=reply_text,
            idempotency_key=f"{state.get('thread_id')}:intro-answer",
            intent=intent,
        )
    return {
        "stage": "handoff",
        "status": "handoff",
        "intent": "unclear",
        "reply_text": None,
        "next_action": {
            "type": "handoff",
            "reason": "intro_intent_unclear",
            "idempotency_key": f"{state.get('thread_id')}:intro-handoff",
        },
        "pending_actions": [],
    }


def should_plan_global_knowledge_answer(state: FunnelGraphState) -> bool:
    metadata = dict(state.get("metadata") or {})
    need = dict(metadata.get("global_knowledge_need") or {})
    if not need.get("needs_answer"):
        return False
    if state.get("stage") == "waiting_intro_reply" and state.get("intent") in {"question", "question_and_agree"}:
        return False
    return bool(metadata.get("faq_reply_text"))


def plan_global_knowledge_turn(state: FunnelGraphState) -> FunnelGraphState:
    stage = state.get("stage") or "waiting_intro_reply"
    answer = str(state.get("metadata", {}).get("faq_reply_text") or FAQ_MANAGER_FALLBACK)
    if stage == "new":
        next_stage = "waiting_intro_reply"
        followup = QUESTION_CONTINUE_PROMPT
    elif stage == "waiting_name":
        next_stage = "waiting_name"
        followup = NAME_QUESTION
    elif stage == "age_check":
        next_stage = "age_check"
        followup = AGE_EXACT_QUESTION
    elif stage == "waiting_intro_reply":
        next_stage = "waiting_intro_reply"
        followup = QUESTION_CONTINUE_PROMPT
    else:
        next_stage = stage
        followup = None
    reply_text = f"{answer}\n\n{followup}" if followup else answer
    return send_text_patch(
        stage=next_stage,
        reply_text=reply_text,
        idempotency_key=f"{state.get('thread_id')}:knowledge-answer:{knowledge_answer_key(state)}",
        intent=str(state.get("intent") or "question"),
    )


def plan_name_turn(state: FunnelGraphState) -> FunnelGraphState:
    name = normalize_candidate_name(latest_inbound_text(state))
    slots = dict(state.get("slots") or {})
    slots["candidate_name"] = name
    preface = AGE_PREFACE_TEMPLATE.format(name=name)
    actions = [
        {
            "type": "send_text",
            "text": preface,
            "idempotency_key": f"{state.get('thread_id')}:age-preface",
        },
        {
            "type": "send_text",
            "text": AGE_EXACT_QUESTION,
            "idempotency_key": f"{state.get('thread_id')}:age-exact-question",
            "delay_seconds": 1,
        },
    ]
    return {
        "stage": "age_check",
        "status": "active",
        "intent": None,
        "slots": slots,
        "reply_text": f"{preface}\n\n{AGE_EXACT_QUESTION}",
        "next_action": None,
        "pending_actions": actions,
    }


def send_text_patch(
    *,
    stage: str,
    reply_text: str,
    idempotency_key: str,
    intent: str | None,
    status: str = "active",
) -> FunnelGraphState:
    return {
        "stage": stage,
        "status": status,
        "intent": intent,
        "reply_text": reply_text,
        "next_action": {
            "type": "send_text",
            "text": reply_text,
            "idempotency_key": idempotency_key,
        },
        "pending_actions": [],
    }


def normalize_classification_result(value: Any) -> IntroClassification:
    if isinstance(value, IntroClassification):
        return value.model_copy(update={"classifier": "llm"})
    if isinstance(value, LLMIntroClassificationResult):
        return IntroClassification(classifier="llm", **value.model_dump())
    if isinstance(value, dict):
        parsed = LLMIntroClassificationResult.model_validate(value)
        return IntroClassification(classifier="llm", **parsed.model_dump())
    return IntroClassification(intent=normalize_intent(value), classifier="llm")


def normalize_faq_answer_result(value: Any) -> FAQAnswerResult:
    if isinstance(value, FAQAnswerResult):
        return value
    if isinstance(value, dict):
        return FAQAnswerResult.model_validate(value)
    if isinstance(value, str):
        return FAQAnswerResult(can_answer_from_faq=bool(value.strip()), reply_text=value)
    return FAQAnswerResult(reply_text=FAQ_MANAGER_FALLBACK, missing_info="invalid_faq_answer_result")


def faq_context_from_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    context = []
    for card in cards:
        content = str(card.get("content") or "").strip()
        if not content:
            continue
        context.append(
            {
                "topic": card.get("topic") or card.get("card_key"),
                "question": card.get("topic") or card.get("card_key"),
                "answer": content,
                "restrictions": card.get("restrictions") or "",
            }
        )
    return context


def normalize_intent(value: object) -> IntroIntent:
    text = str(value or "unclear").strip().lower()
    if text in {"agree", "disagree", "question", "question_and_agree"}:
        return text  # type: ignore[return-value]
    return "unclear"


def normalize_text(text: str) -> str:
    lowered = text.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", lowered)


def contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def normalize_candidate_name(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    cleaned = re.sub(r"^(меня зовут|я|это)\s+", "", cleaned, flags=re.IGNORECASE).strip(" .,!?:;")
    if not cleaned:
        return "Слушай"
    first = cleaned.split()[0].strip(" .,!?:;")
    if not first:
        return "Слушай"
    return first[:40]


def clean_card_answer(text: str) -> str:
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n+", text.strip()) if item.strip()]
    if not paragraphs:
        return ""
    answer = paragraphs[0]
    return re.sub(r"\s+", " ", answer).strip()


def knowledge_answer_key(state: FunnelGraphState) -> str:
    raw = f"{state.get('stage')}:{latest_inbound_text(state)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
