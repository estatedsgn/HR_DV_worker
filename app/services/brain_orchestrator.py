from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.agent_action_log import AgentActionLog
from app.models.dialog import Dialog
from app.models.human_handoff import HumanHandoff
from app.models.lead import Lead
from app.models.message import Message
from app.repositories.lead import LeadRepository
from app.repositories.lead_fact import LeadFactRepository
from app.services.crmchat_diagnostics import redact_value
from app.services.knowledge_base import KnowledgeBaseService
from app.services.llm_adapter import BrainDecision, LLMAdapter, LeadFacts
from app.services.prompt_versions import PromptVersionService


FUNNEL_STATES = {
    "NEW_LEAD",
    "WAITING_FIRST_REPLY",
    "INFO_SENT",
    "WAITING_AFTER_INFO",
    "INTEREST_CLASSIFICATION",
    "QUALIFICATION_STARTED",
    "QUALIFICATION_IN_PROGRESS",
    "OBJECTION_HANDLING",
    "QUALIFIED",
    "READY_FOR_HUMAN",
    "HUMAN_HANDOFF",
    "CONVERTED",
    "LOST",
    "DO_NOT_CONTACT",
}
TERMINAL_STATES = {"CONVERTED", "LOST", "DO_NOT_CONTACT", "HUMAN_HANDOFF"}
REQUIRED_FACTS = ["age", "name", "phone", "iphone_model", "photo_status"]


@dataclass(slots=True, frozen=True)
class BrainStepResult:
    decision: BrainDecision
    should_continue: bool
    reply_text: str | None
    terminal: bool


class BrainOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        *,
        llm_adapter: LLMAdapter | None = None,
    ) -> None:
        self.session = session
        self.llm_adapter = llm_adapter
        self.settings = get_settings()

    async def handle_interest_gate(
        self, *, dialog: Dialog, message_id: str | None = None
    ) -> BrainStepResult:
        message = await self._latest_inbound(dialog.id, message_id=message_id)
        lead = await self._ensure_lead(dialog)
        state_before = lead.funnel_state or "WAITING_FIRST_REPLY"
        rule_decision = classify_interest_by_rules(message.body if message else "", state_before)
        if rule_decision is not None:
            await self.apply_decision(dialog, rule_decision, metadata={"source": "rule_gate"})
            return BrainStepResult(
                decision=rule_decision,
                should_continue=rule_decision.state_after not in TERMINAL_STATES,
                reply_text=None,
                terminal=rule_decision.state_after in TERMINAL_STATES,
            )

        decision = await self._llm_decision(
            dialog,
            state_before="INTEREST_CLASSIFICATION",
            message_limit=min(self.settings.llm_max_input_messages, 8),
        )
        decision = await self._enforce_min_inbound_before_handoff(dialog, decision)
        await self.apply_decision(dialog, decision, metadata=self._last_llm_metadata())
        return BrainStepResult(
            decision=decision,
            should_continue=decision.state_after not in TERMINAL_STATES
            and decision.action in {"send_fixed_info", "wait"},
            reply_text=decision.reply_text if decision.action in {"send_reply", "ask_question"} else None,
            terminal=decision.state_after in TERMINAL_STATES,
        )

    async def run_turn(self, *, dialog: Dialog) -> BrainStepResult:
        lead = await self._ensure_lead(dialog)
        state_before = lead.funnel_state or "QUALIFICATION_IN_PROGRESS"
        deterministic = await self._deterministic_qualification_turn(dialog, lead, state_before)
        if deterministic is not None:
            deterministic = await self._enforce_min_inbound_before_handoff(dialog, deterministic)
            await self.apply_decision(
                dialog,
                deterministic,
                metadata={"provider": "local_rules", "reason": "simple_qualification_answer"},
            )
            return BrainStepResult(
                decision=deterministic,
                should_continue=False,
                reply_text=deterministic.reply_text
                if deterministic.action in {"send_reply", "ask_question"}
                else None,
                terminal=deterministic.state_after in TERMINAL_STATES
                or deterministic.action in {"stop", "handoff"},
            )
        decision = await self._llm_decision(dialog, state_before=state_before)
        decision = await self._enforce_min_inbound_before_handoff(dialog, decision)
        await self.apply_decision(dialog, decision, metadata=self._last_llm_metadata())
        return BrainStepResult(
            decision=decision,
            should_continue=False,
            reply_text=decision.reply_text if decision.action in {"send_reply", "ask_question"} else None,
            terminal=decision.state_after in TERMINAL_STATES or decision.action in {"stop", "handoff"},
        )

    async def apply_decision(
        self,
        dialog: Dialog,
        decision: BrainDecision,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Lead:
        lead = await self._ensure_lead(dialog)
        lead.funnel_state = normalize_state(decision.state_after)
        lead.interest_status = decision.lead_interest
        lead.qualification_status = decision.lead_status
        lead.next_step = next_step_from_decision(decision)
        lead.summary = build_lead_summary(decision)
        if lead.funnel_state == "READY_FOR_HUMAN" and lead.handoff_ready_at is None:
            lead.handoff_ready_at = datetime.now(UTC)
        if lead.funnel_state == "LOST":
            lead.lost_reason = decision.handoff_reason or decision.lead_interest
        if lead.funnel_state == "DO_NOT_CONTACT":
            lead.do_not_contact_reason = decision.handoff_reason or "lead requested no contact"

        await self._upsert_facts(lead, dialog, decision.facts_extracted, decision.confidence)
        if decision.action == "handoff" or lead.funnel_state in {"READY_FOR_HUMAN", "HUMAN_HANDOFF"}:
            await self._ensure_handoff(dialog, decision)
        await self._audit(dialog.id, decision, metadata=metadata or {})
        await self.session.flush()
        return lead

    async def _deterministic_qualification_turn(
        self, dialog: Dialog, lead: Lead, state_before: str
    ) -> BrainDecision | None:
        facts = await LeadFactRepository(self.session).list_by_lead(lead.id)
        messages = await self._messages(dialog.id, limit=8)
        latest_inbound = next(
            (message for message in reversed(messages) if message["direction"] == "inbound"),
            None,
        )
        latest_outbound = next(
            (message for message in reversed(messages) if message["direction"] == "outbound"),
            None,
        )
        if latest_inbound is None or latest_outbound is None:
            return None
        inbound_text = str(latest_inbound.get("body") or "").strip()
        outbound_text = str(latest_outbound.get("body") or "").lower()
        if not should_use_local_qualification(inbound_text):
            return None

        extracted: dict[str, Any] = {}
        if "зовут" in outbound_text or "имя" in outbound_text:
            name = extract_name(inbound_text)
            if name:
                extracted["name"] = name
        elif "лет" in outbound_text or "возраст" in outbound_text:
            age = extract_age(inbound_text)
            if age:
                extracted["age"] = age
        elif "iphone" in outbound_text or "айфон" in outbound_text:
            iphone_model = extract_iphone_model(inbound_text)
            if iphone_model:
                extracted["iphone_model"] = iphone_model
        elif "телефон" in outbound_text or "номер" in outbound_text:
            phone = extract_phone(inbound_text)
            if phone:
                extracted["phone"] = phone
        elif "фото" in outbound_text or "фотограф" in outbound_text:
            extracted["photo_status"] = "provided_or_ready"

        if not extracted:
            return None

        current_missing = missing_required_facts(facts)
        remaining = [fact for fact in current_missing if fact not in extracted]
        reply_text = next_qualification_question(remaining)
        action = "handoff" if reply_text is None else "ask_question"
        state_after = "READY_FOR_HUMAN" if action == "handoff" else "QUALIFICATION_IN_PROGRESS"
        return BrainDecision(
            state_before=state_before,
            state_after=state_after,
            action=action,
            lead_status="qualified" if state_after == "READY_FOR_HUMAN" else "interested",
            lead_interest="positive",
            reply_text=reply_text,
            facts_extracted=LeadFacts.model_validate(extracted),
            missing_required_facts=remaining,
            handoff_reason="Required facts collected" if action == "handoff" else None,
            confidence=0.9,
        )

    async def _llm_decision(
        self, dialog: Dialog, *, state_before: str, message_limit: int | None = None
    ) -> BrainDecision:
        lead = await self._ensure_lead(dialog)
        facts = await LeadFactRepository(self.session).list_by_lead(lead.id)
        messages = await self._messages(dialog.id, limit=message_limit or self.settings.llm_max_input_messages)
        latest_text = next((message["body"] for message in reversed(messages) if message["direction"] == "inbound"), "")
        snippets = await KnowledgeBaseService(self.session).search(latest_text, top_k=5)
        prompt_content, prompt_version = await PromptVersionService(self.session).get_active_or_file(
            name=self.settings.llm_prompt_name,
            version=self.settings.llm_prompt_version,
            file_name="brain_main_v1.md",
        )
        lead_context = {
            "dialog_status": dialog.status,
            "telegram_username": dialog.telegram_username,
            "funnel_state": state_before,
            "lead_status": lead.qualification_status,
            "interest_status": lead.interest_status,
            "known_facts": {
                fact.fact_key: fact.fact_value_json if fact.fact_value_json is not None else fact.fact_value
                for fact in facts
            },
            "missing_required_facts": missing_required_facts(facts),
            "required_facts": REQUIRED_FACTS,
        }
        knowledge_payload = [
            {
                "id": str(result.snippet.id),
                "type": result.snippet.snippet_type,
                "text": result.snippet.text,
                "tags": result.snippet.tags,
                "score": result.score,
            }
            for result in snippets
        ]
        adapter = self.llm_adapter
        if adapter is not None:
            return await adapter.decide_next_action(
                dialog_messages=messages,
                lead_context=lead_context,
                system_prompt=prompt_content,
                prompt_version=prompt_version,
                knowledge_snippets=knowledge_payload,
            )
        async with LLMAdapter() as owned_adapter:
            decision = await owned_adapter.decide_next_action(
                dialog_messages=messages,
                lead_context=lead_context,
                system_prompt=prompt_content,
                prompt_version=prompt_version,
                knowledge_snippets=knowledge_payload,
            )
            self._owned_last_metadata = owned_adapter.last_metadata
            return decision

    async def _ensure_lead(self, dialog: Dialog) -> Lead:
        lead = await LeadRepository(self.session).get_by_dialog(dialog.id)
        if lead is None:
            lead = Lead(
                dialog_id=dialog.id,
                qualification_status="new",
                funnel_state="NEW_LEAD",
            )
            self.session.add(lead)
            await self.session.flush()
        return lead

    async def _upsert_facts(
        self, lead: Lead, dialog: Dialog, facts: LeadFacts, confidence: float | None
    ) -> None:
        payload = facts.model_dump(exclude_none=True)
        repository = LeadFactRepository(self.session)
        for key, value in payload.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, (list, dict)):
                text_value = json.dumps(value, ensure_ascii=False)
                json_value = value if isinstance(value, dict) else {"items": value}
            else:
                text_value = str(value)
                json_value = None
            await repository.upsert_fact(
                lead_id=lead.id,
                dialog_id=dialog.id,
                fact_key=key,
                fact_value=text_value,
                fact_value_json=json_value,
                source="llm",
                confidence=confidence,
            )

    async def _ensure_handoff(self, dialog: Dialog, decision: BrainDecision) -> None:
        result = await self.session.execute(
            select(HumanHandoff)
            .where(HumanHandoff.dialog_id == dialog.id, HumanHandoff.status.in_(["open", "pending"]))
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return
        self.session.add(
            HumanHandoff(
                dialog_id=dialog.id,
                reason=decision.handoff_reason or "Brain marked lead ready for human",
                status="open",
            )
        )

    async def _latest_inbound(self, dialog_id, *, message_id: str | None = None) -> Message | None:
        if message_id:
            message = await self.session.get(Message, message_id)
            if message is not None:
                return message
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id, Message.direction == "inbound")
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _messages(self, dialog_id, *, limit: int) -> list[dict[str, Any]]:
        result = await self.session.execute(
            select(Message)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.sent_at.desc(), Message.created_at.desc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        messages.reverse()
        return [
            {
                "id": str(message.id),
                "direction": message.direction,
                "sender_type": message.sender_type,
                "body": message.body,
                "status": message.status,
                "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            }
            for message in messages
            if isinstance(message.body, str)
        ]

    async def _audit(self, dialog_id, decision: BrainDecision, *, metadata: dict[str, Any]) -> None:
        payload = {
            "decision": decision.model_dump(),
            "llm": metadata,
        }
        self.session.add(
            AgentActionLog(
                dialog_id=dialog_id,
                action_type="brain.decision",
                reasoning=decision.handoff_reason,
                payload_json=json.dumps(redact_value(payload), ensure_ascii=False, default=str),
                status=decision.action,
            )
        )
        await self.session.flush()

    def _last_llm_metadata(self) -> dict[str, Any]:
        if self.llm_adapter is not None:
            return self.llm_adapter.last_metadata
        return getattr(self, "_owned_last_metadata", {})

    async def _enforce_min_inbound_before_handoff(
        self, dialog: Dialog, decision: BrainDecision
    ) -> BrainDecision:
        inbound_count = await self._count_inbound_messages(dialog.id)
        return enforce_min_inbound_before_handoff(
            decision,
            inbound_count=inbound_count,
            min_inbound=self.settings.brain_min_inbound_before_handoff,
        )

    async def _count_inbound_messages(self, dialog_id) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.dialog_id == dialog_id, Message.direction == "inbound")
        )
        return int(result.scalar_one() or 0)


def classify_interest_by_rules(text: str, state_before: str) -> BrainDecision | None:
    normalized = text.strip().lower()
    if not normalized:
        return None
    do_not_contact_markers = [
        "отвали",
        "не пиши",
        "не писать",
        "удали",
        "заблок",
        "иди на",
        "нах",
        "стоп",
        "stop",
    ]
    if any(marker in normalized for marker in do_not_contact_markers):
        return BrainDecision(
            state_before=state_before,
            state_after="DO_NOT_CONTACT",
            action="stop",
            lead_interest="negative",
            lead_status="not_qualified",
            handoff_reason="lead requested no contact",
            confidence=0.95,
        )
    negative_markers = ["нет", "не интересно", "не надо", "херня", "бред", "не хочу"]
    if any(marker == normalized or marker in normalized for marker in negative_markers):
        return BrainDecision(
            state_before=state_before,
            state_after="LOST",
            action="stop",
            lead_interest="negative",
            lead_status="not_qualified",
            handoff_reason="lead is not interested",
            confidence=0.8,
        )
    positive_markers = ["да", "давай", "интересно", "расскажи", "хочу", "можно", "го"]
    if any(marker == normalized or marker in normalized for marker in positive_markers):
        return BrainDecision(
            state_before=state_before,
            state_after="INFO_SENT",
            action="send_fixed_info",
            lead_interest="positive",
            lead_status="interested",
            confidence=0.75,
        )
    return None


def normalize_state(state: str) -> str:
    return state if state in FUNNEL_STATES else "QUALIFICATION_IN_PROGRESS"


def next_step_from_decision(decision: BrainDecision) -> str | None:
    if decision.state_after == "READY_FOR_HUMAN":
        return "human_handoff"
    if decision.action in {"ask_question", "send_reply"}:
        return "await_reply"
    if decision.action == "stop":
        return None
    return decision.action


def build_lead_summary(decision: BrainDecision) -> str | None:
    if decision.reply_text:
        return decision.reply_text
    if decision.handoff_reason:
        return decision.handoff_reason
    return f"{decision.state_after}:{decision.lead_interest}"


def missing_required_facts(facts: list[Any]) -> list[str]:
    present = {fact.fact_key for fact in facts if getattr(fact, "fact_value", None) or getattr(fact, "fact_value_json", None)}
    return [fact for fact in REQUIRED_FACTS if fact not in present]


def enforce_min_inbound_before_handoff(
    decision: BrainDecision, *, inbound_count: int, min_inbound: int
) -> BrainDecision:
    if min_inbound <= 0 or inbound_count >= min_inbound:
        return decision
    if decision.action != "handoff" and decision.state_after not in {
        "READY_FOR_HUMAN",
        "HUMAN_HANDOFF",
        "QUALIFIED",
    }:
        return decision
    return decision.model_copy(
        update={
            "state_after": "QUALIFICATION_IN_PROGRESS",
            "action": "ask_question",
            "decision": "reply",
            "lead_status": "interested",
            "lead_interest": decision.lead_interest
            if decision.lead_interest != "unclear"
            else "positive",
            "reply_text": long_test_followup_question(inbound_count),
            "handoff_reason": None,
            "missing_required_facts": decision.missing_required_facts,
        }
    )


def long_test_followup_question(inbound_count: int) -> str:
    questions = [
        "А если коротко, что для тебя сейчас важнее в работе: деньги, свободный график или спокойный формат?",
        "Какой формат общения тебе комфортнее: быстро по делу или чтобы я объясняла подробнее?",
        "Был ли у тебя уже опыт удаленной работы или пока только присматриваешься?",
        "Что обычно сильнее всего отталкивает тебя в вакансиях?",
        "Если условия подойдут, когда тебе было бы удобно начать?",
        "Хочешь, я сначала расскажу про обязанности или про оплату?",
        "Какой график тебе был бы самым удобным?",
        "Есть ли что-то, что тебе важно сразу уточнить перед тем, как я передам тебя дальше?",
    ]
    return questions[inbound_count % len(questions)]


def should_use_local_qualification(text: str) -> bool:
    normalized = text.strip().lower()
    if not normalized:
        return False
    if "?" in normalized:
        return False
    objection_markers = ["что за", "сколько", "почему", "зачем", "безопас", "обман", "скам", "не понял"]
    return not any(marker in normalized for marker in objection_markers)


def extract_name(text: str) -> str | None:
    cleaned = text.strip().strip(".!,")
    lowered = cleaned.lower()
    prefixes = ["я ", "меня зовут ", "зовут ", "мое имя ", "моё имя "]
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    words = [word.strip(".,!?:;") for word in cleaned.split() if word.strip(".,!?:;")]
    if not words:
        return None
    if len(words) > 3:
        return None
    return " ".join(word.capitalize() for word in words)


def extract_age(text: str) -> str | None:
    for token in text.replace(",", " ").replace(".", " ").split():
        cleaned = "".join(ch for ch in token if ch.isdigit())
        if not cleaned:
            continue
        age = int(cleaned)
        if 14 <= age <= 80:
            return str(age)
    return None


def extract_iphone_model(text: str) -> str | None:
    cleaned = text.strip().strip(".!,")
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if "нет" in lowered and ("iphone" in lowered or "айфон" in lowered):
        return cleaned
    if any(marker in lowered for marker in ["iphone", "айфон", "айф"]):
        return cleaned
    if any(ch.isdigit() for ch in lowered) and len(cleaned) <= 30:
        return cleaned
    return None


def extract_phone(text: str) -> str | None:
    chars = [ch for ch in text if ch.isdigit() or ch == "+"]
    value = "".join(chars)
    digits = "".join(ch for ch in value if ch.isdigit())
    if 10 <= len(digits) <= 15:
        return value
    return None


def next_qualification_question(missing: list[str]) -> str | None:
    if not missing:
        return None
    questions = {
        "name": "Как тебя зовут?",
        "age": "Сколько тебе лет?",
        "iphone_model": "Какая у тебя модель iPhone?",
        "photo_status": "Можешь прислать фото, чтобы я передала дальше без путаницы?",
        "phone": "Оставишь номер телефона, чтобы менеджер мог связаться?",
    }
    for key in REQUIRED_FACTS:
        if key in missing:
            return questions[key]
    return None
