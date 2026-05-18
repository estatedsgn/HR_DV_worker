from typing import Any


class CRMChatConnector:
    """Future adapter for CRMchat webhooks and REST API calls."""

    async def parse_incoming_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def fetch_dialog(self, dialog_id: str) -> dict[str, Any] | None:
        raise NotImplementedError("CRMchat dialog fetching is not implemented yet")
