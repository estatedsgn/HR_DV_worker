from dataclasses import dataclass


@dataclass(slots=True)
class LeadQualificationResult:
    status: str
    score: int | None = None
    summary: str | None = None
    next_step: str | None = None


class LeadQualifier:
    """Classify a dialog and produce lead qualification metadata."""

    async def qualify(self, dialog_id: str) -> LeadQualificationResult:
        return LeadQualificationResult(status="pending")
