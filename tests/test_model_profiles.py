from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.brain_v2.llm_provider import BrainLLMError
from app.services.funnel_graph.reply import ReplyOrchestrator, ReplyResult
from app.services.funnel_graph.semantic import SemanticAnalyzer, SemanticResult


class FakeProfileAdapter:
    def __init__(self, *, fail_components=None):
        self.calls = []
        self.fail_components = set(fail_components or [])

    def has_api_key(self, component):
        return True

    def config_for(self, component, response_model=None):
        models = {
            "semantic_analyzer": "qwen-plus",
            "semantic_analyzer_complex": "qwen3.7-max",
            "reply_orchestrator": "qwen-plus",
            "reply_orchestrator_complex": "qwen3.7-max",
        }
        return SimpleNamespace(model=models.get(component, "unknown"))

    async def complete_json(self, *, component, system_prompt, user_payload, response_model):
        self.calls.append(component)
        if component in self.fail_components:
            raise BrainLLMError("synthetic failure")
        if response_model is SemanticResult:
            return {
                "message_type": "interrupt_question",
                "summary": "question",
                "current_goal_satisfied": False,
                "has_unresolved_interrupt": True,
                "interrupt_type": "question",
                "interrupt_topic": "salary_schedule",
                "interrupt_text": user_payload.get("incoming_message"),
                "facts": {},
                "retrieval_query": user_payload.get("incoming_message") or "",
                "retrieval_topics": ["salary_schedule"],
                "confidence": 0.8,
            }
        return {
            "send_reply": True,
            "reply_mode": "answer_only",
            "outgoing_messages": [{"type": "text", "text": "answer"}],
            "reply_text": "answer",
            "summary": "answer",
            "confidence": 0.8,
        }


def base_state(**overrides):
    state = {
        "stage": "interest_check",
        "current_question": "Рассказать подробнее?",
        "pending_question_text": "Рассказать подробнее?",
        "incoming_message": "давай",
        "candidate_profile": {},
        "message_batch": [{"direction": "inbound", "body": "давай"}],
        "recent_messages": [],
        "metadata": {},
        "send_reply": True,
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_plus_max_router_simple_turn_uses_first_layer_fast_path() -> None:
    adapter = FakeProfileAdapter()
    analyzer = SemanticAnalyzer(
        settings=Settings(MODEL_TEST_PROFILE="plus_max_router"),
        adapter=adapter,
    )

    result = await analyzer.run(base_state())

    assert result.current_goal_satisfied is True
    assert adapter.calls == []
    assert analyzer.last_run_metadata["fast_path"] is True


@pytest.mark.asyncio
async def test_plus_max_router_complex_turn_uses_max_component() -> None:
    adapter = FakeProfileAdapter()
    analyzer = SemanticAnalyzer(
        settings=Settings(MODEL_TEST_PROFILE="plus_max_router"),
        adapter=adapter,
    )

    await analyzer.run(
        base_state(
            incoming_message="это вебкам или onlyfans?",
            message_batch=[{"direction": "inbound", "body": "это вебкам или onlyfans?"}],
        )
    )

    assert adapter.calls == ["semantic_analyzer_complex"]
    assert analyzer.last_run_metadata["model"] == "qwen3.7-max"


@pytest.mark.asyncio
async def test_plus_only_complex_turn_does_not_use_max_component() -> None:
    adapter = FakeProfileAdapter()
    analyzer = SemanticAnalyzer(
        settings=Settings(MODEL_TEST_PROFILE="plus_only"),
        adapter=adapter,
    )

    await analyzer.run(
        base_state(
            incoming_message="а сколько платят и какой график?",
            message_batch=[{"direction": "inbound", "body": "а сколько платят и какой график?"}],
        )
    )

    assert adapter.calls == ["semantic_analyzer"]
    assert analyzer.last_run_metadata["model"] == "qwen-plus"


@pytest.mark.asyncio
async def test_plus_failure_falls_back_to_max_for_complex_semantic() -> None:
    adapter = FakeProfileAdapter(fail_components={"semantic_analyzer"})
    analyzer = SemanticAnalyzer(
        settings=Settings(MODEL_TEST_PROFILE="flash_plus_max"),
        adapter=adapter,
    )

    await analyzer.run(
        base_state(
            incoming_message="привет",
            message_batch=[{"direction": "inbound", "body": "привет"}],
        )
    )

    assert adapter.calls == ["semantic_analyzer", "semantic_analyzer_complex"]
    assert analyzer.last_run_metadata["status"] == "fallback_completed"


@pytest.mark.asyncio
async def test_reply_fast_path_skips_second_prompt_when_policy_can_continue() -> None:
    adapter = FakeProfileAdapter()
    orchestrator = ReplyOrchestrator(
        settings=Settings(MODEL_TEST_PROFILE="fast_max"),
        adapter=adapter,
    )
    state = base_state(
        semantic_result={
            "message_type": "stage_answer",
            "current_goal_satisfied": True,
            "has_unresolved_interrupt": False,
            "facts": {"interest_confirmed": True},
            "confidence": 0.86,
        }
    )

    result = await orchestrator.run(state)

    assert isinstance(result, ReplyResult)
    assert result.outgoing_messages == []
    assert adapter.calls == []
    assert orchestrator.last_run_metadata["fast_path"] is True
