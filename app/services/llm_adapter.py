from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings, get_settings


class LLMConfigurationError(RuntimeError):
    pass


class LLMAPIError(RuntimeError):
    pass


class LeadFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    age: str | int | None = None
    name: str | None = None
    phone: str | None = None
    previous_workplaces: str | list[str] | None = None
    current_activity: str | None = None
    iphone_model: str | None = None
    photo_status: str | None = None


class BrainDecision(BaseModel):
    """Structured output returned by the conversation brain."""

    model_config = ConfigDict(extra="ignore")

    state_before: str = "QUALIFICATION_IN_PROGRESS"
    state_after: str = "QUALIFICATION_IN_PROGRESS"
    action: str = Field(
        default="send_reply",
        description="send_reply | send_fixed_info | ask_question | stop | handoff | wait",
    )
    decision: str | None = Field(default=None, description="Compatibility field: reply | handoff | stop")
    lead_status: str = Field(default="unknown")
    reply_text: str | None = None
    lead_interest: str = Field(default="unclear", description="positive | negative | unclear | objection | question")
    facts_extracted: LeadFacts = Field(default_factory=LeadFacts)
    missing_required_facts: list[str] = Field(default_factory=list)
    handoff_reason: str | None = None
    confidence: float = 0.0

    @model_validator(mode="after")
    def fill_compatibility_fields(self) -> "BrainDecision":
        if self.decision == "handoff" and self.action == "send_reply":
            self.action = "handoff"
        elif self.decision == "stop" and self.action == "send_reply":
            self.action = "stop"
        if self.decision is None:
            if self.action == "handoff":
                self.decision = "handoff"
            elif self.action == "stop":
                self.decision = "stop"
            else:
                self.decision = "reply"
        if self.lead_status == "unknown":
            self.lead_status = lead_status_from_state(self.state_after, self.lead_interest)
        return self


LLMDecision = BrainDecision


class LLMAdapter:
    """Provider-neutral interface for OpenAI Responses and OpenRouter Chat Completions."""

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.last_metadata: dict[str, Any] = {}
        self._owns_http_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            base_url=self._base_url(),
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
        system_prompt: str | None = None,
        prompt_version: str | None = None,
        knowledge_snippets: list[dict[str, Any]] | None = None,
    ) -> BrainDecision:
        if (self.settings.llm_provider or "").lower() == "mock":
            decision = BrainDecision.model_validate_json(self.settings.llm_mock_decision_json)
            self.last_metadata = {
                "provider": "mock",
                "model": self.settings.llm_model,
                "prompt_version": prompt_version,
                "usage": None,
            }
            return decision

        prompt = system_prompt or load_prompt_template()
        limited_messages = dialog_messages[-self.settings.llm_max_input_messages :]
        user_payload = {
            "lead_context": lead_context or {},
            "knowledge_snippets": knowledge_snippets or [],
            "messages": limited_messages,
        }
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
        ]
        endpoint = (self.settings.llm_endpoint or "").lower()
        provider = (self.settings.llm_provider or "").lower()
        if provider == "openrouter" or endpoint in {"chat", "chat_completions"}:
            payload = await self._create_chat_completion(
                messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "brain_decision",
                        "strict": True,
                        "schema": BrainDecision.model_json_schema(),
                    },
                },
            )
            text = extract_chat_completion_text(payload)
        else:
            payload = await self._create_response(
                messages,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "brain_decision",
                        "schema": BrainDecision.model_json_schema(),
                        "strict": True,
                    }
                },
            )
            text = extract_response_text(payload)
        self.last_metadata = build_llm_metadata(
            payload,
            provider=provider or "openai",
            model=self.settings.llm_model,
            prompt_version=prompt_version,
        )
        if text is None:
            raise LLMAPIError("LLM decision response did not contain output text")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMAPIError("LLM decision response was not valid JSON") from exc
        return BrainDecision.model_validate(raw)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.llm_api_key:
            raise LLMConfigurationError("LLM_API_KEY is required for embedding calls")
        response = await self.http_client.post(
            "embeddings",
            json={"model": self.settings.llm_embedding_model, "input": texts},
            headers=self._headers(),
        )
        payload = parse_json_response(response)
        if response.is_error:
            message = extract_openai_error_message(payload) or "LLM embeddings request failed"
            raise LLMAPIError(message)
        data = payload.get("data")
        if not isinstance(data, list):
            raise LLMAPIError("LLM embeddings response did not contain data")
        embeddings: list[list[float]] = []
        for item in data:
            if not isinstance(item, Mapping) or not isinstance(item.get("embedding"), list):
                raise LLMAPIError("LLM embeddings response contained an invalid item")
            embeddings.append([float(value) for value in item["embedding"]])
        return embeddings

    async def _create_response(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> Mapping[str, Any]:
        if not self.settings.llm_api_key:
            raise LLMConfigurationError("LLM_API_KEY is required for OpenAI calls")
        request_payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "input": messages,
            "max_output_tokens": self.settings.llm_max_output_tokens,
            "temperature": self.settings.llm_temperature,
        }
        request_payload.update(kwargs)
        response = await self.http_client.post(
            "/v1/responses",
            json=request_payload,
            headers=self._headers(),
        )
        payload = parse_json_response(response)
        if response.is_error:
            message = extract_openai_error_message(payload) or "OpenAI request failed"
            raise LLMAPIError(message)
        return payload

    async def _create_chat_completion(
        self, messages: list[dict[str, str]], **kwargs: Any
    ) -> Mapping[str, Any]:
        if not self.settings.llm_api_key:
            raise LLMConfigurationError("LLM_API_KEY is required for OpenRouter calls")
        request_payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": self.settings.llm_max_output_tokens,
            "temperature": self.settings.llm_temperature,
        }
        request_payload.update(kwargs)
        response = await self.http_client.post(
            "chat/completions",
            json=request_payload,
            headers=self._headers(),
        )
        payload = parse_json_response(response)
        if response.is_error:
            message = extract_openai_error_message(payload) or "OpenRouter request failed"
            raise LLMAPIError(message)
        return payload

    def _base_url(self) -> str:
        if self.settings.llm_base_url:
            return self.settings.llm_base_url.rstrip("/")
        if (self.settings.llm_provider or "").lower() == "openrouter":
            return "https://openrouter.ai/api/v1"
        return "https://api.openai.com"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }


def lead_status_from_state(state: str, interest: str) -> str:
    if state in {"QUALIFIED", "READY_FOR_HUMAN", "HUMAN_HANDOFF", "CONVERTED"}:
        return "qualified"
    if state in {"LOST", "DO_NOT_CONTACT"}:
        return "not_qualified"
    if interest == "positive":
        return "interested"
    return "unknown"


def parse_json_response(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMAPIError("LLM response was not JSON") from exc
    if not isinstance(payload, Mapping):
        raise LLMAPIError("LLM response must be a JSON object")
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


def extract_chat_completion_text(payload: Mapping[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def build_llm_metadata(
    payload: Mapping[str, Any], *, provider: str, model: str, prompt_version: str | None
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": payload.get("model") or model,
        "prompt_version": prompt_version,
        "usage": payload.get("usage"),
        "response_id": payload.get("id"),
    }


LEGACY_LEAD_DECISION_PROMPT = """You are the legacy HR lead decision assistant.

Return only a structured decision as JSON. Use the provided dialog messages and lead context to choose whether to reply, wait, stop, or hand off. Keep replies short and do not invent missing facts.
"""


def load_prompt_template(name: str = "lead_decision") -> str:
    if name == "lead_decision":
        path = Path(__file__).resolve().parents[1] / "prompts" / "lead_decision.md"
    else:
        path = Path(__file__).resolve().parents[1] / "prompts" / "brain" / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if name == "lead_decision":
        return LEGACY_LEAD_DECISION_PROMPT
    return path.read_text(encoding="utf-8")
