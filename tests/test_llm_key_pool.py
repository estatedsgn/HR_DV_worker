from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

import app.services.llm_key_pool as key_pool_module
from app.core.config import get_settings
from app.services.brain_v2.llm_provider import BrainLLMAdapter, api_key_for_provider
from app.services.llm_key_pool import LLMKeyPool, get_key_pool, is_exhaustion_error


@pytest.fixture(autouse=True)
def _reset_pool_singleton():
    key_pool_module._pool = None
    key_pool_module._pool_signature = None
    yield
    key_pool_module._pool = None
    key_pool_module._pool_signature = None


def _settings(tmp_path, **overrides):
    base = get_settings()
    overrides.setdefault("llm_key_state_path", str(tmp_path / "cursor.json"))
    return base.model_copy(update=overrides)


def test_pool_merges_keys_and_dedups(tmp_path):
    s = _settings(tmp_path, llm_api_keys="k1, k2 ,k3,k1", llm_api_key="k2")
    assert s.llm_api_key_pool() == ["k1", "k2", "k3"]


def test_pool_falls_back_to_single_key(tmp_path):
    s = _settings(tmp_path, llm_api_keys=None, llm_api_key="solo")
    assert s.llm_api_key_pool() == ["solo"]


def test_is_exhaustion_error_matches_quota_messages():
    assert is_exhaustion_error("The free tier of the model has been exhausted.")
    assert is_exhaustion_error("Insufficient balance to call the model")
    assert is_exhaustion_error("Account is in arrearage")
    assert is_exhaustion_error("Invalid API key provided")


def test_is_exhaustion_error_ignores_transient():
    assert not is_exhaustion_error("Read timed out")
    assert not is_exhaustion_error("internal server error")
    assert not is_exhaustion_error(None)


def test_mark_exhausted_advances_and_persists(tmp_path):
    state = tmp_path / "cursor.json"
    pool = LLMKeyPool(["k1", "k2", "k3"], state)
    assert pool.active_key() == "k1"
    assert pool.mark_exhausted("k1") == "k2"
    # A fresh instance reading the same file sees the rotated cursor.
    assert LLMKeyPool(["k1", "k2", "k3"], state).active_key() == "k2"


def test_mark_exhausted_is_idempotent_on_same_key(tmp_path):
    pool = LLMKeyPool(["k1", "k2", "k3"], tmp_path / "cursor.json")
    pool.mark_exhausted("k1")
    # A second process failing on the now-stale key must not skip another slot.
    assert pool.mark_exhausted("k1") == "k2"
    assert pool.active_key() == "k2"


def test_mark_exhausted_wraps_around(tmp_path):
    pool = LLMKeyPool(["k1", "k2"], tmp_path / "cursor.json")
    pool.mark_exhausted("k1")
    assert pool.active_key() == "k2"
    pool.mark_exhausted("k2")
    assert pool.active_key() == "k1"


def test_single_key_pool_never_rotates(tmp_path):
    pool = LLMKeyPool(["solo"], tmp_path / "cursor.json")
    assert pool.mark_exhausted("solo") == "solo"
    assert pool.active_key() == "solo"


def test_api_key_for_provider_uses_active_pool_key(tmp_path):
    s = _settings(tmp_path, llm_api_keys="k1,k2", llm_api_key=None, llm_provider="alibaba")
    assert api_key_for_provider(s, "alibaba") == "k1"
    get_key_pool(s).mark_exhausted("k1")
    assert api_key_for_provider(s, "alibaba") == "k2"


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _FakeClient:
    """Returns an exhaustion error for key k1, success for any other key."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        token = headers["Authorization"].split(" ", 1)[1]
        self.tokens.append(token)
        if token == "k1":
            return _Resp(429, {"error": {"message": "Free tier has been exhausted"}})
        content = json_module_dumps({"reply_text": "ok"})
        return _Resp(200, {"choices": [{"message": {"content": content}}], "model": "qwen", "usage": {}})


def json_module_dumps(obj) -> str:
    return json.dumps(obj)


class _Reply(BaseModel):
    reply_text: str


@pytest.mark.asyncio
async def test_complete_json_rotates_on_exhaustion(tmp_path):
    s = _settings(
        tmp_path,
        llm_api_keys="k1,k2",
        llm_api_key=None,
        llm_provider="alibaba",
        brain_default_provider="alibaba",
        brain_llm_max_retries=1,
    )
    client = _FakeClient()
    adapter = BrainLLMAdapter(settings=s, http_client=client)

    result = await adapter.complete_json(
        component="router",
        system_prompt="sys",
        user_payload={"x": 1},
        response_model=_Reply,
    )

    assert result == {"reply_text": "ok"}
    # First key tried, detected exhausted, rotated to k2 which succeeded.
    assert client.tokens[0] == "k1"
    assert client.tokens[-1] == "k2"
    assert get_key_pool(s).active_key() == "k2"
