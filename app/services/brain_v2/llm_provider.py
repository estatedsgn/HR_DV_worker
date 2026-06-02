from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel

from app.core.config import Settings, get_settings


class BrainLLMError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.25


@dataclass(slots=True, frozen=True)
class LLMComponentConfig:
    component: str
    provider: str
    model: str
    temperature: float
    timeout_seconds: float
    retry_policy: RetryPolicy
    response_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class LLMCallTelemetry:
    component: str
    provider: str
    model: str
    status: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost: float | None = None
    request_json: dict[str, Any] | None = None
    response_json: dict[str, Any] | None = None
    error_message: str | None = None


class BrainLLMAdapter:
    """OpenAI-compatible adapter for Brain V2 components."""

    def __init__(self, settings: Settings | None = None, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.http_client = http_client
        self.telemetry: list[LLMCallTelemetry] = []

    def config_for(self, component: str, response_model: type[BaseModel] | None = None) -> LLMComponentConfig:
        provider = component_provider(self.settings, component)
        model = component_model(self.settings, component)
        schema = response_model.model_json_schema() if response_model is not None else {}
        return LLMComponentConfig(
            component=component,
            provider=provider,
            model=model,
            temperature=self.settings.brain_llm_temperature,
            timeout_seconds=self.settings.brain_llm_timeout_seconds,
            retry_policy=RetryPolicy(max_retries=self.settings.brain_llm_max_retries),
            response_schema=schema,
        )

    def has_api_key(self, component: str) -> bool:
        return bool(api_key_for_provider(self.settings, component_provider(self.settings, component)))

    def has_embedding_api_key(self) -> bool:
        return bool(api_key_for_provider(self.settings, self.settings.default_embedding_provider))

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        provider = self.settings.default_embedding_provider.strip().lower()
        model = self.settings.default_embedding_model
        key = api_key_for_provider(self.settings, provider)
        if not key:
            raise BrainLLMError(f"Missing API key for embedding provider {provider}")
        request_json = {"model": model, "input": texts}
        url = f"{base_url_for_provider(provider, self.settings)}/embeddings"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        try:
            response = await self._post(url, request_json, headers, self.settings.brain_llm_timeout_seconds)
            payload = response.json()
            if response.is_error:
                raise BrainLLMError(extract_error_message(payload) or response.text)
            data = payload.get("data")
            if not isinstance(data, list):
                raise BrainLLMError("Embedding response did not contain data")
            embeddings: list[list[float]] = []
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                    raise BrainLLMError("Embedding response contained an invalid item")
                embeddings.append([float(value) for value in item["embedding"]])
            usage = payload.get("usage") if isinstance(payload, dict) else {}
            self.telemetry.append(
                LLMCallTelemetry(
                    component="embedding",
                    provider=provider,
                    model=str(payload.get("model") or model),
                    status="completed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=optional_int((usage or {}).get("prompt_tokens")),
                    completion_tokens=optional_int((usage or {}).get("completion_tokens")),
                    estimated_cost=None,
                    request_json=request_json,
                    response_json=payload if isinstance(payload, dict) else {"raw": payload},
                )
            )
            return embeddings
        except Exception as exc:
            self.telemetry.append(
                LLMCallTelemetry(
                    component="embedding",
                    provider=provider,
                    model=model,
                    status="failed",
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    request_json=request_json,
                    error_message=str(exc),
                )
            )
            raise

    async def complete_json(
        self,
        *,
        component: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        response_model: type[BaseModel],
    ) -> dict[str, Any]:
        config = self.config_for(component, response_model)
        key = api_key_for_provider(self.settings, config.provider)
        if not key:
            raise BrainLLMError(f"Missing API key for provider {config.provider}")

        request_json: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, default=str)},
            ],
            "temperature": config.temperature,
        }
        if provider_supports_json_schema_response_format(config.provider):
            request_json["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{component}_response",
                    "schema": config.response_schema,
                    "strict": True,
                },
            }
        url = f"{base_url_for_provider(config.provider, self.settings)}/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(config.retry_policy.max_retries + 1):
            try:
                response = await self._post(url, request_json, headers, config.timeout_seconds)
                payload = response.json()
                if response.is_error:
                    raise BrainLLMError(extract_error_message(payload) or response.text)
                content = extract_chat_content(payload)
                if content is None:
                    raise BrainLLMError("LLM response did not contain message content")
                parsed = parse_json_content(content)
                usage = payload.get("usage") if isinstance(payload, dict) else {}
                self.telemetry.append(
                    LLMCallTelemetry(
                        component=component,
                        provider=config.provider,
                        model=str(payload.get("model") or config.model),
                        status="completed",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_tokens=optional_int((usage or {}).get("prompt_tokens")),
                        completion_tokens=optional_int((usage or {}).get("completion_tokens")),
                        estimated_cost=None,
                        request_json=request_json,
                        response_json=payload if isinstance(payload, dict) else {"raw": payload},
                    )
                )
                return parsed
            except Exception as exc:
                last_error = exc
                if attempt >= config.retry_policy.max_retries:
                    break
                await asyncio.sleep(config.retry_policy.base_delay_seconds * (2**attempt))
        self.telemetry.append(
            LLMCallTelemetry(
                component=component,
                provider=config.provider,
                model=config.model,
                status="failed",
                latency_ms=int((time.perf_counter() - started) * 1000),
                request_json=request_json,
                error_message=str(last_error) if last_error else "unknown LLM error",
            )
        )
        raise BrainLLMError(str(last_error) if last_error else "LLM call failed")

    async def _post(
        self, url: str, request_json: dict[str, Any], headers: dict[str, str], timeout_seconds: float
    ) -> httpx.Response:
        if self.http_client is not None:
            return await self.http_client.post(url, json=request_json, headers=headers, timeout=timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            return await client.post(url, json=request_json, headers=headers)


def component_provider(settings: Settings, component: str) -> str:
    from app.services.funnel_graph.model_profiles import component_provider_for_profile

    provider_by_component = {
        "interest_classifier": settings.brain_interest_classifier_provider,
        "router": settings.brain_router_provider,
        "dialogue_brain": settings.brain_dialogue_provider,
        "semantic_analyzer": component_provider_for_profile(settings, "semantic_analyzer"),
        "semantic_analyzer_complex": component_provider_for_profile(settings, "semantic_analyzer_complex"),
        "reply_orchestrator": component_provider_for_profile(settings, "reply_orchestrator"),
        "reply_orchestrator_complex": component_provider_for_profile(settings, "reply_orchestrator_complex"),
        "validator": settings.brain_validator_provider,
        "handoff_summary": settings.brain_handoff_summary_provider,
    }
    return first_non_empty(
        provider_by_component.get(component),
        settings.brain_default_provider,
        settings.llm_provider,
        "openai",
    ).lower()


def component_model(settings: Settings, component: str) -> str:
    from app.services.funnel_graph.model_profiles import component_model_for_profile

    model_by_component = {
        "interest_classifier": settings.brain_interest_classifier_model,
        "router": settings.brain_router_model,
        "dialogue_brain": settings.brain_dialogue_model,
        "semantic_analyzer": component_model_for_profile(settings, "semantic_analyzer"),
        "semantic_analyzer_complex": component_model_for_profile(settings, "semantic_analyzer_complex"),
        "reply_orchestrator": component_model_for_profile(settings, "reply_orchestrator"),
        "reply_orchestrator_complex": component_model_for_profile(settings, "reply_orchestrator_complex"),
        "validator": settings.brain_validator_model,
        "handoff_summary": settings.brain_handoff_summary_model,
    }
    return first_non_empty(
        model_by_component.get(component),
        settings.brain_dialogue_model,
        settings.llm_model,
        "gpt-4.1",
    )


def api_key_for_provider(settings: Settings, provider: str) -> str | None:
    provider = provider.lower()
    if provider == "openai":
        return settings.openai_api_key or settings.llm_api_key
    if provider == "deepseek":
        return settings.deepseek_api_key
    if provider == "groq":
        return settings.groq_api_key
    if provider == "openrouter":
        return settings.openrouter_api_key or settings.llm_api_key
    return settings.llm_api_key


def base_url_for_provider(provider: str, settings: Settings | None = None) -> str:
    provider = provider.lower()
    if settings is not None:
        configured_provider = (settings.llm_provider or "").strip().lower()
        configured_base_url = (settings.llm_base_url or "").strip()
        if configured_base_url and configured_provider == provider:
            return configured_base_url.rstrip("/")
    if provider == "deepseek":
        return "https://api.deepseek.com/v1"
    if provider == "groq":
        return "https://api.groq.com/openai/v1"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "alibaba":
        return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    return "https://api.openai.com/v1"


def first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        stripped = str(value).strip()
        if stripped:
            return stripped
    return ""


def provider_supports_json_schema_response_format(provider: str) -> bool:
    return provider.strip().lower() not in {"alibaba", "dashscope"}


def parse_json_content(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(content))
    if not isinstance(parsed, dict):
        raise BrainLLMError("LLM JSON response was not an object")
    return parsed


def extract_json_object_text(content: str) -> str:
    text = content.strip()
    fence = "```"
    if fence in text:
        parts = text.split(fence)
        for part in parts:
            candidate = part.strip()
            if candidate.lower().startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") and candidate.endswith("}"):
                return candidate
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    raise BrainLLMError("LLM response did not contain a JSON object")


def extract_chat_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


def extract_error_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if payload.get("message"):
        return str(payload["message"])
    return None


def optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
