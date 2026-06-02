from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.services.funnel_graph.funnel_policy import normalize_candidate_profile
from app.services.funnel_graph.knowledge import PROJECT_ROOT
from app.services.funnel_graph.state import FunnelGraphState


class TerminalFunnelRepository:
    """JSON-file repository for terminal-only funnel runs."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or PROJECT_ROOT / "runtime_logs" / "terminal_funnel"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def state_path(self, candidate_id: str) -> Path:
        return self.base_dir / f"{safe_candidate_id(candidate_id)}.json"

    def agent_runs_path(self) -> Path:
        return self.base_dir / "agent_runs.jsonl"

    def load_or_create(self, candidate_id: str) -> FunnelGraphState:
        path = self.state_path(candidate_id)
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["candidate_id"] = candidate_id
            return raw
        return {
            "candidate_id": candidate_id,
            "lead_id": candidate_id,
            "dialog_id": candidate_id,
            "thread_id": f"terminal:{candidate_id}",
            "stage": "interest_check",
            "current_state": "interest_check",
            "status": "active",
            "dialog_status": "active",
            "candidate_profile": normalize_candidate_profile({}),
            "slots": normalize_candidate_profile({}),
            "sent_voice_packs": [],
            "sent_templates": [],
            "recent_messages": [],
            "message_batch": [],
            "metadata": {},
        }

    def reset(self, candidate_id: str) -> None:
        path = self.state_path(candidate_id)
        if path.exists():
            path.unlink()

    def save_turn(
        self,
        candidate_id: str,
        state: FunnelGraphState,
        *,
        incoming_message: str | list[str] | None,
    ) -> None:
        recent_messages = list(state.get("recent_messages") or [])
        incoming_messages = (
            incoming_message
            if isinstance(incoming_message, list)
            else ([incoming_message] if incoming_message else [])
        )
        for text in incoming_messages:
            if text:
                recent_messages.append({"direction": "inbound", "sender_type": "lead", "body": text})
        for message in state.get("outgoing_messages") or []:
            if message.get("type") == "text" and message.get("text"):
                recent_messages.append({"direction": "outbound", "sender_type": "agent", "body": message["text"]})
            elif message.get("type") == "voice_pack" and message.get("voice_pack_id"):
                recent_messages.append(
                    {
                        "direction": "outbound",
                        "sender_type": "agent",
                        "body": f"[voice_pack: {message['voice_pack_id']}]",
                    }
                )
        state_to_save = {
            "candidate_id": candidate_id,
            "lead_id": state.get("lead_id") or candidate_id,
            "dialog_id": state.get("dialog_id") or candidate_id,
            "thread_id": state.get("thread_id") or f"terminal:{candidate_id}",
            "stage": state.get("stage") or "interest_check",
            "current_state": state.get("stage") or "interest_check",
            "previous_state": state.get("previous_state"),
            "resume_state": state.get("resume_state"),
            "status": state.get("status") or "active",
            "dialog_status": state.get("dialog_status") or state.get("status") or "active",
            "current_goal": state.get("current_goal"),
            "current_question": state.get("current_question"),
            "pending_question": state.get("pending_question"),
            "pending_question_text": state.get("pending_question_text"),
            "last_bot_message": state.get("last_bot_message"),
            "last_user_message": state.get("last_user_message"),
            "conversation_history": list(state.get("conversation_history") or [])[-80:],
            "interest_status": state.get("interest_status"),
            "qualification_status": state.get("qualification_status"),
            "last_interrupt_type": state.get("last_interrupt_type"),
            "last_interrupt_topic": state.get("last_interrupt_topic"),
            "waiting_since": state.get("waiting_since"),
            "handoff_required": bool(state.get("handoff_required", False)),
            "next_stage_if_completed": state.get("next_stage_if_completed"),
            "candidate_profile": normalize_candidate_profile(state.get("candidate_profile") or {}),
            "slots": normalize_candidate_profile(state.get("candidate_profile") or {}),
            "sent_voice_packs": list(state.get("sent_voice_packs") or []),
            "sent_templates": list(state.get("sent_templates") or []),
            "recent_messages": recent_messages[-30:],
            "message_batch": [],
            "metadata": dict(state.get("metadata") or {}),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.state_path(candidate_id).write_text(
            json.dumps(state_to_save, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self.append_agent_run(state.get("agent_run") or {})

    def append_agent_run(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        payload = {**payload, "logged_at": datetime.now(UTC).isoformat()}
        with self.agent_runs_path().open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def safe_candidate_id(candidate_id: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in candidate_id)[:120] or "local"
