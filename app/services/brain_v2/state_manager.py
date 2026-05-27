from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brain_v2 import LeadAgendaItem, LeadBrainState, LeadProfileSlot
from app.models.lead import Lead
from app.services.brain_v2.agenda_registry import AgendaRegistry
from app.services.brain_v2.schemas import StatePatch


STAGE_ORDER = [
    "lead_created",
    "waiting_first_reply",
    "first_touch_sent",
    "lead_replied",
    "info_messages_sent",
    "interest_triage",
    "trust",
    "age_gate",
    "basic_info_pack",
    "qualification_faq",
    "company",
    "qualification",
    "personalization",
    "interview_close",
    "pre_schedule_questions",
    "contact_collection",
    "scheduling",
    "confirmation",
    "post_schedule_support",
    "handoff",
    "closed",
]


class StateManager:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_state(self, lead: Lead) -> LeadBrainState:
        result = await self.session.execute(
            select(LeadBrainState).where(LeadBrainState.lead_id == lead.id).limit(1)
        )
        state = result.scalar_one_or_none()
        if state is not None:
            return state
        state = LeadBrainState(
            lead_id=lead.id,
            dialog_id=lead.dialog_id,
            stage="lead_created",
            current_goal="Start HR outreach funnel",
            open_loop=None,
            status="active",
        )
        self.session.add(state)
        await self.session.flush()
        return state

    async def create_agenda_for_interested_lead(self, lead: Lead) -> list[LeadAgendaItem]:
        existing = await self.list_agenda(lead.id)
        existing_keys = {item.item_key for item in existing}
        for spec in AgendaRegistry.standard_items():
            if spec.item_key in existing_keys:
                continue
            self.session.add(
                LeadAgendaItem(
                    lead_id=lead.id,
                    dialog_id=lead.dialog_id,
                    item_key=spec.item_key,
                    stage=spec.stage,
                    priority=spec.priority,
                    required=spec.required,
                    status="pending",
                    completion_rule=spec.completion_rule,
                    default_question=spec.default_question,
                    slot_key=spec.slot_key,
                    next_stage_hint=spec.next_stage_hint,
                )
            )
        await self.session.flush()
        return await self.list_agenda(lead.id)

    async def list_agenda(self, lead_id) -> list[LeadAgendaItem]:
        result = await self.session.execute(
            select(LeadAgendaItem)
            .where(LeadAgendaItem.lead_id == lead_id)
            .order_by(LeadAgendaItem.priority.asc(), LeadAgendaItem.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_slots(self, lead_id) -> list[LeadProfileSlot]:
        result = await self.session.execute(
            select(LeadProfileSlot)
            .where(LeadProfileSlot.lead_id == lead_id)
            .order_by(LeadProfileSlot.slot_key.asc())
        )
        return list(result.scalars().all())

    async def apply_slot_patch(
        self,
        lead: Lead,
        slot_patch: dict[str, Any],
        *,
        source: str = "brain",
        confidence: float | None = None,
    ) -> None:
        if not slot_patch:
            return
        existing = {slot.slot_key: slot for slot in await self.list_slots(lead.id)}
        for key, value in slot_patch.items():
            if value in (None, "", [], {}):
                continue
            slot_value_json = value if isinstance(value, dict) else {"items": value} if isinstance(value, list) else None
            slot_value = None if slot_value_json is not None else str(value)
            slot = existing.get(key)
            if slot is None:
                self.session.add(
                    LeadProfileSlot(
                        lead_id=lead.id,
                        dialog_id=lead.dialog_id,
                        slot_key=key,
                        slot_value=slot_value,
                        slot_value_json=slot_value_json,
                        source=source,
                        confidence=confidence,
                    )
                )
            else:
                slot.slot_value = slot_value
                slot.slot_value_json = slot_value_json
                slot.source = source
                slot.confidence = confidence
        await self.session.flush()
        await self.mark_agenda_from_slots(lead.id)

    async def apply_state_patch(
        self,
        state: LeadBrainState,
        patch: StatePatch | dict[str, Any] | None,
    ) -> None:
        if patch is None:
            return
        if isinstance(patch, dict):
            patch = StatePatch.model_validate(patch)
        if patch.stage:
            state.stage = normalize_stage(patch.stage)
            if state.stage == "closed":
                state.status = "closed"
        if patch.open_loop is not None:
            state.open_loop = patch.open_loop
        if patch.current_goal is not None:
            state.current_goal = patch.current_goal
        if patch.agenda_updates:
            await self.update_agenda_statuses(state.lead_id, patch.agenda_updates)
        await self.session.flush()

    async def update_stage(self, state: LeadBrainState, stage: str) -> None:
        state.stage = normalize_stage(stage)
        if state.stage == "closed":
            state.status = "closed"
        await self.session.flush()

    async def update_open_loop(self, state: LeadBrainState, open_loop: dict[str, Any] | None) -> None:
        state.open_loop = open_loop
        await self.session.flush()

    async def update_agenda_statuses(self, lead_id, updates: dict[str, str]) -> None:
        agenda = {item.item_key: item for item in await self.list_agenda(lead_id)}
        for item_key, status in updates.items():
            item = agenda.get(item_key)
            if item is not None:
                item.status = normalize_agenda_status(status)
        await self.session.flush()

    async def mark_agenda_from_slots(self, lead_id) -> None:
        slots = {slot.slot_key for slot in await self.list_slots(lead_id)}
        for item in await self.list_agenda(lead_id):
            if item.slot_key and item.slot_key in slots and item.status in {"pending", "active", "blocked"}:
                item.status = "done"
        await self.session.flush()

    async def snapshot(self, lead: Lead) -> dict[str, Any]:
        state = await self.get_or_create_state(lead)
        slots = await self.list_slots(lead.id)
        agenda = await self.list_agenda(lead.id)
        return {
            "lead_id": str(lead.id),
            "dialog_id": str(lead.dialog_id),
            "stage": state.stage,
            "current_goal": state.current_goal,
            "open_loop": state.open_loop,
            "profile_slots": slots_to_dict(slots),
            "agenda_items": [agenda_item_to_dict(item) for item in agenda],
        }


def normalize_stage(stage: str) -> str:
    return stage if stage in STAGE_ORDER else "qualification"


def normalize_agenda_status(status: str) -> str:
    return status if status in {"pending", "active", "done", "blocked", "skipped"} else "pending"


def slots_to_dict(slots: list[LeadProfileSlot]) -> dict[str, Any]:
    return {
        slot.slot_key: slot.slot_value_json if slot.slot_value_json is not None else slot.slot_value
        for slot in slots
    }


def agenda_item_to_dict(item: LeadAgendaItem) -> dict[str, Any]:
    return {
        "item_key": item.item_key,
        "stage": item.stage,
        "priority": item.priority,
        "required": item.required,
        "status": item.status,
        "completion_rule": item.completion_rule,
        "default_question": item.default_question,
        "slot_key": item.slot_key,
        "next_stage_hint": item.next_stage_hint,
    }


def next_stage_after(stage: str) -> str:
    try:
        index = STAGE_ORDER.index(stage)
    except ValueError:
        return "qualification"
    if index >= len(STAGE_ORDER) - 1:
        return "closed"
    return STAGE_ORDER[index + 1]
