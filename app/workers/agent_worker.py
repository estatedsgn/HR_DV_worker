import logging
from dataclasses import dataclass
from typing import Any

from app.services import ConversationMemory, HumanHandoffService, LeadQualifier, MessageHandler, Sendler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentPipelineResult:
    status: str
    action: str | None = None
    handoff_required: bool = False


class AgentWorker:
    """Skeleton for the future end-to-end dialog processing pipeline."""

    def __init__(
        self,
        message_handler: MessageHandler,
        conversation_memory: ConversationMemory,
        lead_qualifier: LeadQualifier,
        human_handoff_service: HumanHandoffService,
        sendler: Sendler,
    ) -> None:
        self.message_handler = message_handler
        self.conversation_memory = conversation_memory
        self.lead_qualifier = lead_qualifier
        self.human_handoff_service = human_handoff_service
        self.sendler = sendler

    async def process_incoming_message(self, payload: dict[str, Any]) -> AgentPipelineResult:
        """Describe the planned pipeline without performing external integrations yet."""

        normalized = await self.message_handler.normalize_incoming(payload)
        dialog_id = str(normalized.get("dialog_id", ""))
        message_text = str(normalized.get("text", ""))

        # 1. Accept incoming CRMchat/Telegram message payload.
        # 2. Persist inbound Message through MessageRepository.
        # 3. Update dialog history and memory summary.
        await self.conversation_memory.update_history(dialog_id, [message_text] if message_text else [])

        # 4. Qualify or refresh Lead state.
        qualification = await self.lead_qualifier.qualify(dialog_id)

        # 5. Choose the next agent action from memory, qualification, and policy.
        action = "handoff" if qualification.status == "requires_human" else "wait"

        # 6. Escalate to a human when policy requires it.
        handoff = await self.human_handoff_service.should_handoff(dialog_id)
        if handoff.required:
            logger.info("human handoff required", extra={"dialog_id": dialog_id})
            return AgentPipelineResult(status="handoff", action="handoff", handoff_required=True)

        # 7. Send a response through Sendler when the chosen action requires an outbound message.
        # 8. Log action/outbound results via LogRepository.
        logger.info("agent pipeline planned", extra={"dialog_id": dialog_id})
        return AgentPipelineResult(status="planned", action=action, handoff_required=False)
