from dataclasses import dataclass


@dataclass(slots=True)
class NewMessageHandler:
    async def on_new_message(self, *, dialog_id: str, crmchat_message_id: str) -> None:
        # Placeholder hook for future agent processing pipeline.
        print(f"NEW_MESSAGE_NEEDS_PROCESSING dialog_id={dialog_id} message_id={crmchat_message_id}")
