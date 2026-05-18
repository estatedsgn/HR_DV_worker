from typing import Any


class MessageHandler:
    """Normalize incoming/outgoing message payloads for the agent pipeline."""

    async def normalize_incoming(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload
