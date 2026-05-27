from scripts.run_controlled_funnel import (
    DEFAULT_VOICE_RECORDING_SECONDS,
    ControlledClassifierResult,
    age_decision_from_classifier,
    apply_speed_defaults,
    classify_age_reply,
    deterministic_turn_classify,
    ensure_age_question,
    ensure_required_text,
    extract_age,
    fallback_controlled_interest_reply,
    has_candidate_question,
    is_interest,
    is_refusal,
)
from argparse import Namespace


def test_controlled_funnel_classifies_first_reply_and_age() -> None:
    assert is_interest("\u0434\u0430 \u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438")
    assert is_interest("\u0447\u0442\u043e \u0437\u0430 \u0440\u0430\u0431\u043e\u0442\u0430?")
    assert is_refusal("\u043d\u0435 \u043f\u0438\u0448\u0438")
    assert extract_age("\u043c\u043d\u0435 19") == 19
    assert extract_age("\u043d\u0435\u0442 18") == 17


def test_age_gate_accepts_contextual_yes_answers() -> None:
    assert classify_age_reply("\u0434\u0430", age_context=True).is_18_plus is True
    assert classify_age_reply("da", age_context=True).is_18_plus is True
    assert classify_age_reply("yes", age_context=True).is_18_plus is True
    assert classify_age_reply("\u0434\u0430, \u043c\u043d\u0435 \u0443\u0436\u0435 \u0435\u0441\u0442\u044c", age_context=True).age == 18
    assert classify_age_reply("\u0434\u0430 \u0435\u0441\u0442\u044c", age_context=True).accepted is True
    assert classify_age_reply("\u0434\u0430", age_context=False).accepted is False


def test_age_gate_closes_underage_answers() -> None:
    decision = classify_age_reply("\u043d\u0435\u0442", age_context=True)

    assert decision.accepted is True
    assert decision.is_18_plus is False
    assert decision.close_reason == "underage"
    assert classify_age_reply("\u043c\u043d\u0435 16", age_context=True).is_18_plus is False


def test_classifier_uses_deterministic_result_when_confident() -> None:
    result = deterministic_turn_classify("\u0434\u0430 \u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438", phase_name="await_first_reply", age_context=False)

    assert result.intent == "interested"
    assert result.confidence >= 0.8


def test_age_classifier_result_maps_to_age_gate_decision() -> None:
    decision = age_decision_from_classifier(
        ControlledClassifierResult(intent="interested", age=None, is_18_plus=True, confidence=0.7),
        age_context=True,
    )

    assert decision.accepted is True
    assert decision.age == 18
    assert decision.is_18_plus is True


def test_plain_yes_outside_age_context_does_not_become_age_classifier_result() -> None:
    result = deterministic_turn_classify("\u0434\u0430", phase_name="await_first_reply", age_context=False)
    decision = age_decision_from_classifier(result, age_context=False)

    assert result.intent == "interested"
    assert decision.accepted is False


def test_interest_llm_fallback_keeps_required_age_question() -> None:
    fallback = fallback_controlled_interest_reply("\u0434\u0430\u0432\u0430\u0439")

    assert fallback.intent == "interested"
    assert "\u0435\u0441\u0442\u044c 18" in fallback.text
    assert ensure_age_question("\u043a\u043e\u0440\u043e\u0442\u043a\u043e \u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0443").count("\u0435\u0441\u0442\u044c 18") == 1


def test_script_helpers_prioritize_questions_and_required_next_step() -> None:
    assert has_candidate_question("\u0430 \u0447\u0442\u043e \u043f\u043e \u043e\u043f\u043b\u0430\u0442\u0435") is True
    assert has_candidate_question("\u0434\u0430 \u0434\u0430\u0432\u0430\u0439") is False
    assert ensure_required_text("\u043e\u0442\u0432\u0435\u0442", "\u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0448\u0430\u0433") == "\u043e\u0442\u0432\u0435\u0442. \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0448\u0430\u0433"


def test_fast_profile_uses_long_voice_recording_wait() -> None:
    args = Namespace(
        speed_profile="fast",
        poll_interval_seconds=None,
        voice_gap_seconds=None,
        typing_delay_seconds=None,
        voice_recording_delay_seconds=None,
        brain_debounce_seconds=None,
    )

    apply_speed_defaults(args)

    assert args.poll_interval_seconds == 1.0
    assert args.voice_recording_delay_seconds == DEFAULT_VOICE_RECORDING_SECONDS
