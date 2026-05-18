from dataclasses import dataclass


@dataclass(slots=True)
class HandoffDecision:
    required: bool
    reason: str | None = None


class HumanHandoffService:
    """Detect and create handoffs to human operators."""

    async def should_handoff(self, dialog_id: str) -> HandoffDecision:
        return HandoffDecision(required=False)
