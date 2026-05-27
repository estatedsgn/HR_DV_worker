from app.services.brain_orchestrator import (
    classify_interest_by_rules,
    enforce_min_inbound_before_handoff,
    missing_required_facts,
)
from app.services.llm_adapter import BrainDecision


class FakeFact:
    def __init__(self, key: str, value: str | None):
        self.fact_key = key
        self.fact_value = value
        self.fact_value_json = None


def test_rule_gate_marks_do_not_contact_without_llm() -> None:
    decision = classify_interest_by_rules("Не пиши мне больше", "WAITING_FIRST_REPLY")

    assert decision is not None
    assert decision.action == "stop"
    assert decision.state_after == "DO_NOT_CONTACT"
    assert decision.lead_status == "not_qualified"


def test_rule_gate_marks_positive_interest_for_fixed_info() -> None:
    decision = classify_interest_by_rules("давай расскажи", "WAITING_FIRST_REPLY")

    assert decision is not None
    assert decision.action == "send_fixed_info"
    assert decision.state_after == "INFO_SENT"
    assert decision.lead_interest == "positive"


def test_missing_required_facts_ignores_empty_values() -> None:
    missing = missing_required_facts(
        [
            FakeFact("age", "22"),
            FakeFact("phone", None),
            FakeFact("name", "Аня"),
        ]
    )

    assert "age" not in missing
    assert "name" not in missing
    assert "phone" in missing


def test_min_inbound_guard_keeps_test_dialog_open_before_threshold() -> None:
    decision = BrainDecision(
        state_before="QUALIFICATION_IN_PROGRESS",
        state_after="READY_FOR_HUMAN",
        action="handoff",
        lead_interest="positive",
        lead_status="qualified",
        handoff_reason="Required facts collected",
    )

    guarded = enforce_min_inbound_before_handoff(
        decision,
        inbound_count=4,
        min_inbound=20,
    )

    assert guarded.action == "ask_question"
    assert guarded.state_after == "QUALIFICATION_IN_PROGRESS"
    assert guarded.lead_status == "interested"
    assert guarded.reply_text
    assert guarded.handoff_reason is None


def test_min_inbound_guard_allows_handoff_after_threshold() -> None:
    decision = BrainDecision(
        state_before="QUALIFICATION_IN_PROGRESS",
        state_after="READY_FOR_HUMAN",
        action="handoff",
        lead_interest="positive",
        lead_status="qualified",
    )

    guarded = enforce_min_inbound_before_handoff(
        decision,
        inbound_count=20,
        min_inbound=20,
    )

    assert guarded is decision
