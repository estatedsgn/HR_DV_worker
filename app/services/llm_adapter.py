from typing import Any


class LLMAdapter:
    """Provider-neutral interface for future LLM calls."""

    async def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise NotImplementedError("LLM completion is not implemented yet")
