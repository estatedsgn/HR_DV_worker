import asyncio
import json

from app.services.agent_policy import AgentPolicyService
from app.services.dialog_state import DialogStateSnapshot


class FakeSession:
    def __init__(self):
        self.logs = []
        self.commits = 0

    def add(self, instance):
        self.logs.append(instance)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


def snapshot(**kwargs):
    base = dict(
        dialog_id="d1",
        dialog_status="open",
        lead_status="qualified",
        active_handoff_status=None,
        last_agent_action=None,
        messages=[{"body": "hello"}],
    )
    base.update(kwargs)
    return DialogStateSnapshot(**base)


def test_policy_allow() -> None:
    session = FakeSession()
    decision = asyncio.run(AgentPolicyService(session).evaluate_reply(snapshot()))
    assert decision.decision == "allow"
    assert decision.reason_code == "safe_to_reply"
    payload = json.loads(session.logs[-1].payload_json)
    assert payload["reason_code"] == "safe_to_reply"


def test_policy_deny_risk_flag() -> None:
    session = FakeSession()
    decision = asyncio.run(
        AgentPolicyService(session).evaluate_reply(snapshot(messages=[{"body": "this is scam"}]))
    )
    assert decision.decision == "deny"
    assert decision.reason_code == "risk_flag_detected"


def test_policy_require_handoff() -> None:
    session = FakeSession()
    decision = asyncio.run(
        AgentPolicyService(session).evaluate_reply(snapshot(active_handoff_status="open"))
    )
    assert decision.decision == "require_handoff"
    assert decision.reason_code == "handoff_active"
