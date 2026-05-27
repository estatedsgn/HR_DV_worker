from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


BrainStage = Literal[
    "lead_created",
    "waiting_first_reply",
    "first_touch_sent",
    "lead_replied",
    "info_messages_sent",
    "interest_triage",
    "trust",
    "age_gate",
    "basic_info_pack",
    "qualification_faq",
    "company",
    "qualification",
    "personalization",
    "interview_close",
    "pre_schedule_questions",
    "contact_collection",
    "scheduling",
    "confirmation",
    "post_schedule_support",
    "handoff",
    "closed",
]

ExecutorActionType = Literal[
    "send_message",
    "schedule_followup",
    "handoff",
    "close_lost",
    "do_nothing",
]

DialogueMove = Literal[
    "answer_and_reassure",
    "answer_and_resume_open_loop",
    "collect_missing_slot",
    "soft_qualify",
    "handle_trust_objection",
    "personalize_offer",
    "move_to_interview",
    "collect_contact",
    "schedule_interview",
    "handoff_to_human",
    "close_lost",
    "pause_and_follow_up",
]


class BrainMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    direction: str = "inbound"
    sender_type: str | None = None
    body: str
    sent_at: str | None = None


class AgendaItemSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    item_key: str
    stage: str
    priority: int
    required: bool = True
    status: str = "pending"
    completion_rule: str
    default_question: str
    slot_key: str | None = None
    next_stage_hint: str | None = None


class BrainGatewayInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lead_id: str
    conversation_id: str
    incoming_message: BrainMessage
    recent_messages: list[BrainMessage] = Field(default_factory=list)
    message_batch: list[BrainMessage] = Field(default_factory=list)
    current_state: dict[str, Any] = Field(default_factory=dict)
    profile_slots: dict[str, Any] = Field(default_factory=dict)
    agenda_items: list[AgendaItemSnapshot] = Field(default_factory=list)
    channel_meta: dict[str, Any] = Field(default_factory=dict)


class RouterResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_intent: str = "unclear"
    secondary_intents: list[str] = Field(default_factory=list)
    dialogue_act: str = "statement"
    has_candidate_question: bool = False
    question_topics: list[str] = Field(default_factory=list)
    objection_topics: list[str] = Field(default_factory=list)
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    retrieval_topics: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class RetrievedKnowledgeCard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    card_key: str
    stage: str | None = None
    topic: str | None = None
    content: str
    score: float = 0.0
    match_reasons: list[str] = Field(default_factory=list)


class StatePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    stage: str | None = None
    open_loop: dict[str, Any] | None = None
    current_goal: str | None = None
    agenda_updates: dict[str, str] = Field(default_factory=dict)


class ExecutorAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ExecutorActionType = "do_nothing"
    text: str | None = None
    followup_seconds: int | None = None
    handoff_reason: str | None = None
    close_reason: str | None = None


class DialogueBrainDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interpretation: str = ""
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    dialogue_decision: dict[str, Any] = Field(default_factory=dict)
    knowledge_usage: list[dict[str, Any]] = Field(default_factory=list)
    response: str | None = None
    state_patch: StatePatch = Field(default_factory=StatePatch)
    executor_action: ExecutorAction = Field(default_factory=ExecutorAction)
    confidence: float = 0.0

    @property
    def dialogue_move(self) -> str:
        return str(self.dialogue_decision.get("dialogue_move") or "collect_missing_slot")


class ValidatorResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["pass", "revise", "handoff"] = "pass"
    quality_score: float = 1.0
    issues: list[str] = Field(default_factory=list)
    revision_instruction: str | None = None
    approved_text: str | None = None
    handoff_reason: str | None = None


class BrainGatewayDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision_id: str
    router_result: RouterResult
    retrieved_cards: list[RetrievedKnowledgeCard]
    brain_decision: DialogueBrainDecision
    validator_result: ValidatorResult
    executor_action: ExecutorAction
    state_patch: StatePatch
    brain_run_id: str | None = None
