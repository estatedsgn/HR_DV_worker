from app.models.agent_action_log import AgentActionLog
from app.models.human_handoff import HumanHandoff
from app.models.outbound_send_log import OutboundSendLog
from app.repositories.base import BaseRepository


class LogRepository:
    """Repository facade for agent action, outbound send, and handoff audit logs."""

    def __init__(self, session) -> None:
        self.agent_actions = BaseRepository[AgentActionLog](session)
        self.agent_actions.model = AgentActionLog
        self.outbound_sends = BaseRepository[OutboundSendLog](session)
        self.outbound_sends.model = OutboundSendLog
        self.handoffs = BaseRepository[HumanHandoff](session)
        self.handoffs.model = HumanHandoff
