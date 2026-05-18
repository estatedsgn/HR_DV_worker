from collections.abc import Sequence


class ConversationMemory:
    """Maintain compact dialog history and summaries for agent decisions."""

    async def update_history(self, dialog_id: str, messages: Sequence[str]) -> str | None:
        return "\n".join(messages) if messages else None
