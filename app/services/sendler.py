from dataclasses import dataclass


@dataclass(slots=True)
class SendResult:
    status: str
    external_message_id: str | None = None
    error_message: str | None = None


class Sendler:
    """Future outbound sender with anti-ban limits and throttling policies."""

    async def send_message(self, dialog_id: str, text: str) -> SendResult:
        raise NotImplementedError("Outbound sending is not implemented yet")
