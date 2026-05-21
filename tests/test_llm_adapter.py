import json

import httpx
import pytest

from app.core.config import Settings
from app.services.llm_adapter import LLMAdapter, LLMDecision, extract_response_text, load_prompt_template


def test_extract_response_text_from_responses_payload() -> None:
    payload = {
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": '{"decision":"reply"}'},
                ]
            }
        ]
    }

    assert extract_response_text(payload) == '{"decision":"reply"}'


@pytest.mark.asyncio
async def test_llm_adapter_parses_structured_decision() -> None:
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "decision": "reply",
                                        "lead_status": "qualified",
                                        "reply_text": "Готово, передаю менеджеру.",
                                        "handoff_reason": None,
                                        "confidence": 0.82,
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    }
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.openai.com",
    )
    adapter = LLMAdapter(
        settings=Settings(LLM_API_KEY="test-key", LLM_MODEL="gpt-test"),
        http_client=client,
    )

    decision = await adapter.decide_next_action(
        dialog_messages=[{"direction": "inbound", "body": "Интересно"}],
        lead_context={"lead_status": "new"},
    )

    assert isinstance(decision, LLMDecision)
    assert decision.decision == "reply"
    assert decision.lead_status == "qualified"
    assert captured["body"]["model"] == "gpt-test"
    assert captured["body"]["text"]["format"]["type"] == "json_schema"
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_adapter_mock_mode_uses_configured_decision() -> None:
    adapter = LLMAdapter(
        settings=Settings(
            LLM_PROVIDER="mock",
            LLM_API_KEY=None,
            LLM_MOCK_DECISION_JSON=json.dumps(
                {
                    "decision": "handoff",
                    "lead_status": "interested",
                    "reply_text": None,
                    "handoff_reason": "Need manager",
                    "confidence": 0.9,
                }
            ),
        )
    )

    decision = await adapter.decide_next_action(dialog_messages=[], lead_context={})

    assert decision.decision == "handoff"
    assert decision.handoff_reason == "Need manager"


def test_prompt_template_is_loaded_from_file() -> None:
    prompt = load_prompt_template()

    assert "Return only a structured decision" in prompt
