"""Import all ORM models so Alembic can discover Base.metadata tables."""

from app.models.account import Account
from app.models.agent_action_log import AgentActionLog
from app.models.crmchat_webhook_event import CRMChatWebhookEvent
from app.models.dialog import Dialog
from app.models.human_handoff import HumanHandoff
from app.models.inbound_event import InboundEvent
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_send_log import OutboundSendLog
from app.models.telegram_polling_run import TelegramPollingRun
from app.models.telegram_dialog_target import TelegramDialogTarget

__all__ = [
    "Account",
    "AgentActionLog",
    "CRMChatWebhookEvent",
    "Dialog",
    "HumanHandoff",
    "InboundEvent",
    "Lead",
    "Message",
    "OutboundSendLog",
    "TelegramPollingRun",
    "TelegramDialogTarget",
]
