"""Import all ORM models so Alembic can discover Base.metadata tables."""

from app.models.account import Account
from app.models.agent_action_log import AgentActionLog
from app.models.brain_v2 import (
    BrainRun,
    KnowledgeCard,
    LeadAgendaItem,
    LeadBrainState,
    LeadProfileSlot,
    LLMCall,
    RetrievalEvent,
)
from app.models.campaign import Campaign, CampaignStep
from app.models.crmchat_webhook_event import CRMChatWebhookEvent
from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.human_handoff import HumanHandoff
from app.models.inbound_event import InboundEvent
from app.models.knowledge_snippet import KnowledgeSnippet
from app.models.lead import Lead
from app.models.lead_fact import LeadFact
from app.models.lead_intake_event import LeadIntakeEvent
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.models.outbound_send_log import OutboundSendLog
from app.models.prompt_version import PromptVersion
from app.models.telegram_polling_run import TelegramPollingRun

__all__ = [
    "Account",
    "AgentActionLog",
    "BrainRun",
    "Campaign",
    "CampaignStep",
    "CRMChatWebhookEvent",
    "Dialog",
    "DialogSequenceRun",
    "LeadFunnelRuntime",
    "HumanHandoff",
    "InboundEvent",
    "KnowledgeCard",
    "KnowledgeSnippet",
    "LeadAgendaItem",
    "LeadBrainState",
    "Lead",
    "LeadFact",
    "LeadProfileSlot",
    "LeadIntakeEvent",
    "LLMCall",
    "Message",
    "OutboundJob",
    "OutboundSendLog",
    "PromptVersion",
    "RetrievalEvent",
    "TelegramPollingRun",
]
