from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings


class LLMConfigurationError(RuntimeError):
    pass


class LLMAPIError(RuntimeError):
    pass


class LLMDecision(BaseModel):
    decision: str = Field(description="reply | handoff | stop")
    lead_status: str = Field(description="new | interested | qualified | not_qualified | unknown")
    reply_text: str | None = None
    handoff_reason: str | None = None
    confidence: float


class LLMAdapter:
    """Provider-neutral interface backed by OpenAI Responses API for V1."""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            base_url="https://api.openai.com",
            timeout=30,
        )

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def __aenter__(self) -> "LLMAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        payload = await self._create_response(messages, **kwargs)
        text = extract_response_text(payload)
        if text is None:
            raise LLMAPIError("OpenAI response did not contain output text")
        return text

    async def decide_next_action(
        self,
        *,
        dialog_messages: list[dict[str, Any]],
        lead_context: dict[str, Any] | None = None,
    ) -> LLMDecision:
        if (self.settings.llm_provider or "").lower() == "mock":
            return LLMDecision.model_validate_json(self.settings.llm_mock_decision_json)
        system_prompt = load_prompt_template()
        user_payload = {
            "lead_context": lead_context or {},
            "messages": dialog_messages,
        }
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        payload = await self._create_response(
            messages,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "lead_decision",
                    "schema": LLMDecision.model_json_schema(),
                    "strict": True,
                }
            },
        )
        text = extract_response_text(payload)
        if text is None:
            raise LLMAPIError("OpenAI decision response did not contain output text")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMAPIError("OpenAI decision response was not valid JSON") from exc
        return LLMDecision.model_validate(raw)

    async def _create_response(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> Mapping[str, Any]:
        if not self.settings.llm_api_key:
            raise LLMConfigurationError("LLM_API_KEY is required for OpenAI calls")
        request_payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "input": messages,
        }
        request_payload.update(kwargs)
        response = await self.http_client.post(
            "/v1/responses",
            json=request_payload,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMAPIError("OpenAI response was not JSON") from exc
        if not isinstance(payload, Mapping):
            raise LLMAPIError("OpenAI response must be a JSON object")
        if response.is_error:
            message = extract_openai_error_message(payload) or "OpenAI request failed"
            raise LLMAPIError(message)
        return payload


def extract_openai_error_message(payload: Mapping[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, Mapping) and error.get("message"):
        return str(error["message"])
    return None


def extract_response_text(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct

    output = payload.get("output")
    if not isinstance(output, list):
        return None
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, Mapping):
                continue
            text = content_item.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks) if chunks else None


def load_prompt_template() -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / "lead_decision.md"
    return path.read_text(encoding="utf-8")
