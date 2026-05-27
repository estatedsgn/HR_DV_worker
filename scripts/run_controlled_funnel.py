from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text, update

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal, engine
from app.models.account import Account
from app.models.brain_v2 import BrainRun, KnowledgeCard, LeadAgendaItem, LeadBrainState, LeadProfileSlot
from app.models.dialog import Dialog
from app.models.inbound_event import InboundEvent
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.account_sync import AccountSyncService
from app.services.brain_v2.llm_provider import BrainLLMAdapter, BrainLLMError
from app.services.brain_v2.state_manager import StateManager
from app.services.campaign_sequence import build_peer_from_dialog
from app.services.crmchat_connector import CRMChatConnector
from app.services.inbound_queue_worker import InboundQueueWorker
from app.services.outbound_queue_worker import OutboundQueueWorker
from app.services.telegram_polling import TelegramPollingService
from app.services.knowledge_base import tokenize


DEFAULT_MANIFEST = Path("data/voice_intro/voice_intro_manifest.json")
DEFAULT_VOICE_RECORDING_SECONDS = 50.0
CONTROL_KEY = "controlled_funnel_v1"
GREETING_CARD_KEY = "first_touch.streamer_offer"
AGE_QUESTION = "Скажи, пожалуйста, тебе уже есть 18?"
TEMPLATES = {
    "age_question": "Скажи, пожалуйста, тебе уже есть 18?",
    "age_retry": "Это просто формальность для допуска к формату. Скажи, пожалуйста, тебе уже есть 18?",
    "first_reply_clarify": "Могу коротко рассказать, если тебе в целом интересно. Продолжить?",
    "basic_info_prompt": "Если интересна наша сфера, давай расскажу про зп и график.",
    "deep_info_clarify": "Правильно понимаю, тебе рассказать подробнее про оплату, график и как проходит работа?",
}
AGE_QUESTION = TEMPLATES["age_question"]


class ControlledLLMReply(BaseModel):
    text: str = Field(default="")
    slot_patch: dict[str, Any] = Field(default_factory=dict)
    intent: str = "interested"


class ControlledClassifierResult(BaseModel):
    intent: str = "unclear"
    age: int | None = None
    is_18_plus: bool | None = None
    has_question: bool = False
    confidence: float = 0.0


@dataclass(slots=True, frozen=True)
class AgeGateDecision:
    age: int | None
    is_18_plus: bool | None
    accepted: bool
    close_reason: str | None = None


@dataclass(slots=True)
class PhaseState:
    phase: str
    started_at: str
    greeting_message_id: str | None = None
    greeting_sent_at: str | None = None
    first_reply_message_id: str | None = None
    age_question_message_id: str | None = None
    age_question_sent_at: str | None = None
    age_reply_message_id: str | None = None
    llm_interest_reply_message_id: str | None = None
    last_processed_inbound_message_id: str | None = None
    age_retry_anchor_message_id: str | None = None
    question_answer_message_id: str | None = None
    question_answer_sent_at: str | None = None
    basic_prompt_message_id: str | None = None
    basic_prompt_sent_at: str | None = None
    basic_interest_message_id: str | None = None
    voice_index: int = 0
    voice_pack: str | None = None
    last_voice_job_id: str | None = None
    last_voice_sent_at: str | None = None
    brain_started_at: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Controlled autonomous HR funnel run for one Telegram user.")
    parser.add_argument("--username", required=True, help="Target username, for example @iamnekiy.")
    parser.add_argument("--allow-real-send", action="store_true", help="Required for real Telegram sends.")
    parser.add_argument("--reset", action="store_true", help="Reset Brain V2 state and queued work for this dialog.")
    parser.add_argument("--cycles", type=int, default=120)
    parser.add_argument("--speed-profile", choices=["fast", "natural"], default="fast")
    parser.add_argument("--poll-interval-seconds", type=float, default=None)
    parser.add_argument("--voice-gap-seconds", type=float, default=None)
    parser.add_argument("--typing-delay-seconds", type=float, default=None)
    parser.add_argument("--voice-recording-delay-seconds", type=float, default=None)
    parser.add_argument("--brain-debounce-seconds", type=int, default=None)
    parser.add_argument("--fast-pacing-seconds", type=int, default=0)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--require-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    apply_speed_defaults(args)
    return args


def apply_speed_defaults(args: argparse.Namespace) -> None:
    fast = args.speed_profile == "fast"
    if args.poll_interval_seconds is None:
        args.poll_interval_seconds = 1.0 if fast else 3.0
    if args.voice_gap_seconds is None:
        args.voice_gap_seconds = 2.0 if fast else 5.0
    if args.typing_delay_seconds is None:
        args.typing_delay_seconds = 0.5 if fast else 2.0
    if args.voice_recording_delay_seconds is None:
        args.voice_recording_delay_seconds = DEFAULT_VOICE_RECORDING_SECONDS
    if args.brain_debounce_seconds is None:
        args.brain_debounce_seconds = 2 if fast else 10


async def main() -> None:
    args = parse_args()
    if not args.allow_real_send and not args.dry_run:
        raise SystemExit("refused: pass --allow-real-send or --dry-run")
    configure_live_brain(args.brain_debounce_seconds)
    settings = get_settings()
    if args.require_llm and not BrainLLMAdapter(settings=settings).has_api_key("dialogue_brain"):
        raise SystemExit("refused: dialogue_brain provider has no API key")

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    voice_items = sorted(manifest.get("items") or [], key=lambda item: int(item.get("order") or 100))
    if not voice_items:
        raise SystemExit("voice manifest has no items")

    lock_key = f"controlled_funnel:{normalize_username(args.username)}"
    async with engine.connect() as lock_connection:
        locked = await acquire_advisory_lock(lock_connection, lock_key)
        if not locked:
            raise SystemExit(f"refused: another controlled funnel runner is already active for {args.username}")
        try:
            await run_locked(args, settings, manifest_path, voice_items)
        finally:
            await release_advisory_lock(lock_connection, lock_key)


async def run_locked(args: argparse.Namespace, settings, manifest_path: Path, voice_items: list[dict[str, Any]]) -> None:
    async with CRMChatConnector(settings=settings) as connector:
        for cycle in range(1, args.cycles + 1):
            started = datetime.now(UTC)
            phase = None
            try:
                async with AsyncSessionLocal() as session:
                    if cycle == 1:
                        sync = await AccountSyncService(session, connector=connector).sync_active_accounts()
                        print(f"[{cycle}] account_sync active={sync.active_remote} created={sync.created} updated={sync.updated}")
                        await set_fast_pacing(session, args.fast_pacing_seconds)

                    poll_result = await TelegramPollingService(
                        session,
                        connector=connector,
                        only_username=args.username,
                        mark_read=False,
                    ).poll_all_active_accounts_once()

                    dialog = await find_target_dialog(session, args.username)
                    if dialog is None:
                        print(f"[{cycle}] no dialog for {args.username}; poll={poll_result.status}")
                        await session.commit()
                        await asyncio.sleep(args.poll_interval_seconds)
                        continue
                    if cycle == 1 and args.reset:
                        await reset_dialog_run(session, dialog)

                    phase = await load_or_create_phase(session, dialog)
                    phase = await drive_phase(
                        session=session,
                        dialog=dialog,
                        phase=phase,
                        manifest_path=manifest_path,
                        voice_items=voice_items,
                        voice_gap_seconds=args.voice_gap_seconds,
                        voice_recording_delay_seconds=args.voice_recording_delay_seconds,
                        dry_run=args.dry_run,
                    )
                    await store_phase(session, dialog, phase)

                    inbound_result = None
                    if phase.phase == "brain_active" and not args.dry_run:
                        inbound_result = await InboundQueueWorker(
                            session,
                            lease_owner="controlled-funnel-inbound",
                        ).process_queued_batch(limit=50)

                    outbound_result = None
                    if not args.dry_run:
                        outbound_result = await OutboundQueueWorker(
                            session,
                            connector=connector,
                            lease_owner="controlled-funnel-outbound",
                            allow_real_send=True,
                            typing_delay_seconds=args.typing_delay_seconds,
                        ).process_queued_batch(limit=5)

                    terminal = await latest_terminal_state(session, dialog)
                    await session.commit()
            except Exception as exc:
                elapsed = (datetime.now(UTC) - started).total_seconds()
                print(f"[{cycle}] recoverable_error={type(exc).__name__}: {exc} elapsed={elapsed:.1f}s")
                await asyncio.sleep(max(args.poll_interval_seconds, 5.0))
                continue

            elapsed = (datetime.now(UTC) - started).total_seconds()
            print(
                f"[{cycle}] phase={phase.phase} voices={phase.voice_index}/{len(voice_items)} "
                f"poll_created={poll_result.messages_created} "
                f"inbound={format_inbound(inbound_result)} outbound={format_outbound(outbound_result)} "
                f"terminal={terminal or '-'} elapsed={elapsed:.1f}s"
            )
            if terminal in {"closed", "handoff"}:
                return
            await asyncio.sleep(args.poll_interval_seconds)


def configure_live_brain(brain_debounce_seconds: int | None = None) -> None:
    os.environ["BRAIN_V2_ENABLED"] = "true"
    os.environ["BRAIN_SHADOW_MODE"] = "false"
    os.environ.setdefault("BRAIN_ENABLE_VALIDATOR", "true")
    os.environ.setdefault("BRAIN_DEFAULT_PROVIDER", "openrouter")
    os.environ.setdefault("BRAIN_ROUTER_PROVIDER", "openrouter")
    os.environ.setdefault("BRAIN_DIALOGUE_PROVIDER", "openrouter")
    os.environ.setdefault("BRAIN_VALIDATOR_PROVIDER", "openrouter")
    os.environ.setdefault("BRAIN_ROUTER_MODEL", "openai/gpt-4.1-mini")
    os.environ.setdefault("BRAIN_DIALOGUE_MODEL", "openai/gpt-4.1-mini")
    os.environ.setdefault("BRAIN_VALIDATOR_MODEL", "openai/gpt-4.1-mini")
    if brain_debounce_seconds is not None:
        os.environ["BRAIN_INBOUND_DEBOUNCE_SECONDS"] = str(max(0, int(brain_debounce_seconds)))
    get_settings.cache_clear()


async def drive_phase(
    *,
    session,
    dialog: Dialog,
    phase: PhaseState,
    manifest_path: Path,
    voice_items: list[dict[str, Any]],
    voice_gap_seconds: float,
    voice_recording_delay_seconds: float | None,
    dry_run: bool,
) -> PhaseState:
    if phase.phase == "new":
        existing_greeting = await latest_controlled_text_message(
            session,
            dialog,
            "controlled:first_touch",
            after=parse_dt(phase.started_at),
        )
        if existing_greeting is not None:
            phase.phase = "await_first_reply"
            phase.greeting_message_id = str(existing_greeting.id)
            sent_at = existing_greeting.sent_at or existing_greeting.updated_at
            phase.greeting_sent_at = sent_at.isoformat() if sent_at else None
            print("  greeting already exists; waiting for first reply")
            return phase
        text = await greeting_text(session)
        message_id = None if dry_run else await enqueue_text(
            session,
            dialog,
            text,
            "controlled:first_touch",
            after=parse_dt(phase.started_at),
        )
        phase.phase = "await_first_reply"
        phase.greeting_message_id = str(message_id) if message_id else None
        print(f"  queued greeting: {text}")
        return phase

    if phase.phase == "await_first_reply":
        sent_at = await message_sent_at(session, phase.greeting_message_id)
        if sent_at is not None and phase.greeting_sent_at is None:
            phase.greeting_sent_at = sent_at.isoformat()
        reply = await latest_inbound_after(session, dialog.id, parse_dt(phase.greeting_sent_at))
        if reply is None:
            return phase
        if phase.last_processed_inbound_message_id == str(reply.id):
            return phase
        phase.first_reply_message_id = str(reply.id)
        phase.last_processed_inbound_message_id = str(reply.id)
        await mark_inbound_event_processed(session, reply.id, "controlled first reply consumed")
        classifier = await classify_controlled_turn(
            session,
            dialog,
            reply,
            phase_name="await_first_reply",
            age_context=False,
        )
        if classifier.intent == "refusal":
            await close_controlled_lead(session, dialog, "not_interested_after_first_touch")
            phase.phase = "closed"
            return phase
        if classifier.intent not in {"interested", "question_or_objection"}:
            message_id = None if dry_run else await enqueue_text(
                session,
                dialog,
                TEMPLATES["first_reply_clarify"],
                "controlled:first_reply_clarify",
                after=parse_dt(phase.started_at),
                reply_to_message_id=str(reply.id),
            )
            phase.greeting_message_id = phase.greeting_message_id or (str(message_id) if message_id else None)
            return phase
        await prepare_age_gate(session, dialog)
        age_decision = classify_age_reply(reply.body, age_context=False)
        if age_decision.accepted and age_decision.is_18_plus:
            await apply_profile_slot(session, dialog, {"age": age_decision.age or 18, "is_18_plus": True})
            await prepare_basic_info_pack(session, dialog)
            phase.age_reply_message_id = str(reply.id)
            phase.phase = "sending_basic_info_pack"
            phase.voice_pack = "basic"
            phase.voice_index = 0
            phase.last_voice_job_id = None
            phase.last_voice_sent_at = None
            return phase
        llm_reply = await controlled_llm_interest_reply(session, dialog, reply)
        response_text = ensure_age_question(llm_reply.text)
        message_id = None if dry_run else await enqueue_text(
            session,
            dialog,
            response_text,
            "controlled:llm_interest_gate",
            after=parse_dt(phase.started_at),
            reply_to_message_id=str(reply.id),
        )
        phase.llm_interest_reply_message_id = str(message_id) if message_id else None
        phase.age_question_message_id = phase.llm_interest_reply_message_id
        phase.phase = "llm_interest_gate"
        return phase

    if phase.phase == "llm_interest_gate":
        sent_at = await message_sent_at(session, phase.llm_interest_reply_message_id or phase.age_question_message_id)
        if sent_at is not None and phase.age_question_sent_at is None:
            phase.age_question_sent_at = sent_at.isoformat()
        phase.phase = "await_age"
        return phase

    if phase.phase == "await_age":
        sent_at = await message_sent_at(session, phase.age_question_message_id)
        if sent_at is None and phase.age_question_message_id:
            status = await outbound_message_job_status(session, phase.age_question_message_id)
            if status in {"cancelled", "failed", "dead_letter", "blocked"}:
                message_id = None if dry_run else await enqueue_text(
                    session,
                    dialog,
                    AGE_QUESTION,
                    "controlled:llm_interest_gate",
                    after=parse_dt(phase.started_at),
                    reply_to_message_id=phase.last_processed_inbound_message_id,
                )
                phase.age_question_message_id = str(message_id) if message_id else None
                phase.llm_interest_reply_message_id = phase.age_question_message_id
                phase.age_question_sent_at = None
                return phase
        if sent_at is not None and phase.age_question_sent_at is None:
            phase.age_question_sent_at = sent_at.isoformat()
        reply = await latest_inbound_after(session, dialog.id, parse_dt(phase.age_question_sent_at))
        if reply is None:
            return phase
        if phase.last_processed_inbound_message_id == str(reply.id):
            return phase
        phase.age_reply_message_id = str(reply.id)
        phase.last_processed_inbound_message_id = str(reply.id)
        age_decision = classify_age_reply(reply.body, age_context=True)
        if not age_decision.accepted:
            classifier = await classify_controlled_turn(
                session,
                dialog,
                reply,
                phase_name="await_age",
                age_context=True,
            )
            age_decision = age_decision_from_classifier(classifier, age_context=True)
        if not age_decision.accepted:
            if phase.age_retry_anchor_message_id == str(reply.id):
                return phase
            response_text = TEMPLATES["age_retry"]
            if has_candidate_question(reply.body):
                response_text = await controlled_llm_answer_text(
                    session,
                    dialog,
                    reply,
                    phase_name="age_gate",
                    required_next_step=TEMPLATES["age_retry"],
                    append_required_next=True,
                )
            message_id = None if dry_run else await enqueue_text(
                session,
                dialog,
                response_text,
                "controlled:age_retry",
                after=parse_dt(phase.started_at),
                reply_to_message_id=str(reply.id),
            )
            phase.age_retry_anchor_message_id = str(reply.id)
            phase.age_question_message_id = str(message_id) if message_id else None
            phase.age_question_sent_at = None
            return phase
        if age_decision.is_18_plus is False:
            await apply_profile_slot(session, dialog, {"age": age_decision.age or 17, "is_18_plus": False})
            await close_controlled_lead(session, dialog, age_decision.close_reason or "underage")
            phase.phase = "closed"
            return phase
        await apply_profile_slot(session, dialog, {"age": age_decision.age or 18, "is_18_plus": True})
        await prepare_basic_info_pack(session, dialog)
        if has_candidate_question(reply.body):
            message_id = None if dry_run else await enqueue_text(
                session,
                dialog,
                await controlled_llm_answer_text(
                    session,
                    dialog,
                    reply,
                    phase_name="age_gate",
                    required_next_step="После ответа перейди к базовым голосовым о формате работы.",
                    append_required_next=False,
                ),
                f"controlled:question_answer:{reply.id}",
                after=parse_dt(phase.started_at),
                reply_to_message_id=str(reply.id),
            )
            phase.question_answer_message_id = str(message_id) if message_id else None
        phase.phase = "sending_basic_info_pack"
        phase.voice_pack = "basic"
        phase.voice_index = 0
        phase.last_voice_job_id = None
        phase.last_voice_sent_at = None
        return phase

    if phase.phase == "sending_basic_info_pack":
        if await maybe_answer_new_question_before_script(
            session=session,
            dialog=dialog,
            phase=phase,
            dry_run=dry_run,
            phase_name="basic_info_pack",
            required_next_step="После ответа продолжи базовый voice pack о формате работы.",
        ):
            return phase
        phase = await send_voice_pack_step(
            session=session,
            dialog=dialog,
            phase=phase,
            manifest_path=manifest_path,
            voice_items=[item for item in voice_items if int(item.get("order") or 0) <= 2],
            voice_gap_seconds=voice_gap_seconds,
            voice_recording_delay_seconds=voice_recording_delay_seconds,
            dry_run=dry_run,
            pack_name="basic",
        )
        if phase.phase != "sending_basic_info_pack":
            return phase
        if phase.voice_index >= 2 and not phase.last_voice_job_id:
            message_id = None if dry_run else await enqueue_text(
                session,
                dialog,
                "Если интересна наша сфера, давай расскажу про зп и график.",
                "controlled:basic_info_prompt",
                after=parse_dt(phase.started_at),
                reply_to_message_id=phase.last_processed_inbound_message_id,
            )
            phase.basic_prompt_message_id = str(message_id) if message_id else None
            phase.phase = "await_deep_info_interest"
        return phase

    if phase.phase == "await_deep_info_interest":
        sent_at = await message_sent_at(session, phase.basic_prompt_message_id)
        if sent_at is not None and phase.basic_prompt_sent_at is None:
            phase.basic_prompt_sent_at = sent_at.isoformat()
        reply = await latest_inbound_after(session, dialog.id, parse_dt(phase.basic_prompt_sent_at))
        if reply is None:
            return phase
        phase.basic_interest_message_id = str(reply.id)
        if has_candidate_question(reply.body):
            if phase.last_processed_inbound_message_id == str(reply.id):
                return phase
            phase.last_processed_inbound_message_id = str(reply.id)
            message_id = None if dry_run else await enqueue_text(
                session,
                dialog,
                await controlled_llm_answer_text(
                    session,
                    dialog,
                    reply,
                    phase_name="await_deep_info_interest",
                    required_next_step=TEMPLATES["basic_info_prompt"],
                    append_required_next=True,
                ),
                f"controlled:question_answer:{reply.id}",
                after=parse_dt(phase.started_at),
                reply_to_message_id=str(reply.id),
            )
            phase.question_answer_message_id = str(message_id) if message_id else None
            phase.basic_prompt_sent_at = None
            phase.basic_prompt_message_id = phase.question_answer_message_id
            return phase
        if is_refusal(reply.body):
            await close_controlled_lead(session, dialog, "not_interested_after_basic_info")
            phase.phase = "closed"
            return phase
        if not is_interest(reply.body):
            await enqueue_text(
                session,
                dialog,
                "Правильно понимаю, тебе рассказать подробнее про оплату, график и как проходит работа?",
                "controlled:deep_info_clarify",
                after=parse_dt(phase.started_at),
                reply_to_message_id=str(reply.id),
            )
            return phase
        await prepare_deep_info_pack(session, dialog)
        phase.phase = "sending_deep_info_pack"
        phase.voice_pack = "deep"
        phase.voice_index = 0
        phase.last_voice_job_id = None
        phase.last_voice_sent_at = None
        return phase

    if phase.phase == "sending_deep_info_pack":
        if await maybe_answer_new_question_before_script(
            session=session,
            dialog=dialog,
            phase=phase,
            dry_run=dry_run,
            phase_name="deep_info_pack",
            required_next_step="После ответа продолжи подробный voice pack про платформы, процесс и следующие шаги.",
        ):
            return phase
        phase = await send_voice_pack_step(
            session=session,
            dialog=dialog,
            phase=phase,
            manifest_path=manifest_path,
            voice_items=[item for item in voice_items if int(item.get("order") or 0) > 2],
            voice_gap_seconds=voice_gap_seconds,
            voice_recording_delay_seconds=voice_recording_delay_seconds,
            dry_run=dry_run,
            pack_name="deep",
        )
        if phase.phase == "sending_deep_info_pack" and phase.voice_index >= 2 and not phase.last_voice_job_id:
            await prepare_qualification_faq(session, dialog)
            phase.phase = "brain_active"
            phase.brain_started_at = datetime.now(UTC).isoformat()
        return phase

    if phase.phase == "sending_voice_pack":
        # Backward-compatible recovery for runs started before the corrected funnel.
        phase.phase = "await_age"
        phase.voice_index = 0
        phase.voice_pack = None
        phase.last_voice_job_id = None
        phase.last_voice_sent_at = None
        return phase

    if phase.phase == "legacy_sending_voice_pack":
        activity_anchor = (
            parse_dt(phase.last_voice_sent_at)
            or await message_created_at(session, phase.first_reply_message_id)
            or parse_dt(phase.greeting_sent_at)
        )
        if await candidate_spoke_after(session, dialog.id, activity_anchor):
            phase.phase = "brain_active"
            phase.brain_started_at = datetime.now(UTC).isoformat()
            return phase
        if phase.last_voice_job_id:
            status, sent_at = await outbound_job_status(session, phase.last_voice_job_id)
            if status not in {"sent", "cancelled", "blocked", "failed", "dead_letter"}:
                return phase
            if sent_at is not None and phase.last_voice_sent_at is None:
                phase.last_voice_sent_at = sent_at.isoformat()
            if sent_at and datetime.now(UTC) < sent_at + timedelta(seconds=voice_gap_seconds):
                return phase
            phase.last_voice_job_id = None
        if phase.voice_index >= len(voice_items):
            phase.phase = "await_after_info"
            return phase
        item = voice_items[phase.voice_index]
        job_id = None if dry_run else await enqueue_voice(
            session,
            dialog,
            manifest_path.parent,
            item,
            after=parse_dt(phase.started_at),
            recording_delay_seconds=voice_recording_delay_seconds,
            reply_to_message_id=phase.last_processed_inbound_message_id,
        )
        phase.voice_index += 1
        phase.last_voice_job_id = str(job_id) if job_id else None
        phase.last_voice_sent_at = None
        print(f"  queued voice #{item.get('order')}: {item.get('topic')}")
        return phase

    if phase.phase == "await_after_info":
        after = parse_dt(phase.last_voice_sent_at) or parse_dt(phase.greeting_sent_at)
        reply = await latest_inbound_after(session, dialog.id, after)
        if reply is None:
            return phase
        phase.phase = "brain_active"
        phase.brain_started_at = datetime.now(UTC).isoformat()
        return phase

    return phase


async def send_voice_pack_step(
    *,
    session,
    dialog: Dialog,
    phase: PhaseState,
    manifest_path: Path,
    voice_items: list[dict[str, Any]],
    voice_gap_seconds: float,
    voice_recording_delay_seconds: float | None,
    dry_run: bool,
    pack_name: str,
) -> PhaseState:
    if phase.last_voice_job_id:
        status, sent_at = await outbound_job_status(session, phase.last_voice_job_id)
        if status not in {"sent", "cancelled", "blocked", "failed", "dead_letter"}:
            return phase
        if sent_at is not None and phase.last_voice_sent_at is None:
            phase.last_voice_sent_at = sent_at.isoformat()
        if sent_at and datetime.now(UTC) < sent_at + timedelta(seconds=voice_gap_seconds):
            return phase
        phase.last_voice_job_id = None
    if phase.voice_index >= len(voice_items):
        return phase
    item = voice_items[phase.voice_index]
    job_id = None if dry_run else await enqueue_voice(
        session,
        dialog,
        manifest_path.parent,
        item,
        after=parse_dt(phase.started_at),
        pack_name=pack_name,
        recording_delay_seconds=voice_recording_delay_seconds,
        reply_to_message_id=phase.last_processed_inbound_message_id,
    )
    phase.voice_index += 1
    phase.voice_pack = pack_name
    phase.last_voice_job_id = str(job_id) if job_id else None
    phase.last_voice_sent_at = None
    print(f"  queued {pack_name} voice #{item.get('order')}: {item.get('topic')}")
    return phase


async def greeting_text(session) -> str:
    result = await session.execute(select(KnowledgeCard.content).where(KnowledgeCard.card_key == GREETING_CARD_KEY).limit(1))
    content = result.scalar_one_or_none()
    if not content:
        return "привет! у меня есть интересное предложение по удаленной работе. если тебе интересно, могу коротко рассказать подробности."
    parts = [part.strip() for part in str(content).split("\n\n") if part.strip()]
    return parts[-1] if parts else str(content).strip()


async def controlled_llm_interest_reply(session, dialog: Dialog, reply: Message) -> ControlledLLMReply:
    return await controlled_llm_reply(
        session,
        dialog,
        reply,
        phase_name="llm_interest_gate",
        required_next_step=AGE_QUESTION,
        append_required_next=True,
    )


async def controlled_llm_answer_text(
    session,
    dialog: Dialog,
    reply: Message,
    *,
    phase_name: str,
    required_next_step: str | None,
    append_required_next: bool,
) -> str:
    llm_reply = await controlled_llm_reply(
        session,
        dialog,
        reply,
        phase_name=phase_name,
        required_next_step=required_next_step,
        append_required_next=append_required_next,
    )
    text = llm_reply.text.strip() or fallback_controlled_answer(reply.body, required_next_step, append_required_next)
    if append_required_next and required_next_step:
        return ensure_required_text(text, required_next_step)
    return text


async def controlled_llm_reply(
    session,
    dialog: Dialog,
    reply: Message,
    *,
    phase_name: str,
    required_next_step: str | None,
    append_required_next: bool,
) -> ControlledLLMReply:
    adapter = BrainLLMAdapter()
    if adapter.has_api_key("dialogue_brain"):
        try:
            cards = await controlled_context_cards(session, query=reply.body, phase=phase_name)
            payload = await adapter.complete_json(
                component="dialogue_brain",
                system_prompt=CONTROLLED_LLM_PROMPT,
                user_payload={
                    "phase": phase_name,
                    "candidate_message": reply.body,
                    "telegram_username": dialog.telegram_username,
                    "required_next_step": required_next_step,
                    "append_required_next": append_required_next,
                    "templates": TEMPLATES,
                    "context_cards": cards,
                },
                response_model=ControlledLLMReply,
            )
            return ControlledLLMReply.model_validate(payload)
        except BrainLLMError as exc:
            print(f"  controlled llm fallback: {exc}")
    if phase_name == "llm_interest_gate":
        return fallback_controlled_interest_reply(reply.body)
    return ControlledLLMReply(
        text=fallback_controlled_answer(reply.body, required_next_step, append_required_next),
        intent="question_answer",
    )


async def classify_controlled_turn(
    session,
    dialog: Dialog,
    reply: Message,
    *,
    phase_name: str,
    age_context: bool,
) -> ControlledClassifierResult:
    deterministic = deterministic_turn_classify(reply.body, phase_name=phase_name, age_context=age_context)
    if deterministic.confidence >= 0.8:
        return deterministic

    adapter = BrainLLMAdapter()
    if adapter.has_api_key("dialogue_brain"):
        try:
            cards = await controlled_context_cards(session, query=reply.body, phase=phase_name)
            payload = await adapter.complete_json(
                component="dialogue_brain",
                system_prompt=CONTROLLED_CLASSIFIER_PROMPT,
                user_payload={
                    "phase": phase_name,
                    "candidate_message": reply.body,
                    "telegram_username": dialog.telegram_username,
                    "age_context": age_context,
                    "templates": TEMPLATES,
                    "context_cards": cards,
                },
                response_model=ControlledClassifierResult,
            )
            llm_result = ControlledClassifierResult.model_validate(payload)
            if llm_result.confidence >= 0.45:
                return llm_result
        except BrainLLMError as exc:
            print(f"  controlled classifier fallback: {exc}")
    return deterministic


CONTROLLED_CLASSIFIER_PROMPT = """You classify one candidate Telegram message for a strict HR funnel.
Return only JSON matching the schema.
intent must be one of: interested, refusal, question_or_objection, unclear.
If age_context is true and the candidate confirms they are 18+, set
is_18_plus=true and age=18 unless a real age is stated. If they state an age
below 18 or deny being 18+, set is_18_plus=false and age to the stated age or 17.
If age_context is false, do not treat a plain yes as age confirmation.
Set has_question=true when the message asks for information or raises suspicion.
Do not write a reply text."""


def deterministic_turn_classify(text: str | None, *, phase_name: str, age_context: bool) -> ControlledClassifierResult:
    age_decision = classify_age_reply(text, age_context=age_context)
    if age_decision.accepted:
        return ControlledClassifierResult(
            intent="interested" if age_decision.is_18_plus else "refusal",
            age=age_decision.age,
            is_18_plus=age_decision.is_18_plus,
            has_question=has_candidate_question(text),
            confidence=0.95,
        )
    if is_refusal(text):
        return ControlledClassifierResult(
            intent="refusal",
            has_question=has_candidate_question(text),
            confidence=0.9,
        )
    if is_interest(text):
        return ControlledClassifierResult(
            intent="question_or_objection" if has_candidate_question(text) else "interested",
            has_question=has_candidate_question(text),
            confidence=0.85,
        )
    return ControlledClassifierResult(
        intent="question_or_objection" if has_candidate_question(text) else "unclear",
        has_question=has_candidate_question(text),
        confidence=0.55 if has_candidate_question(text) else 0.0,
    )


def age_decision_from_classifier(result: ControlledClassifierResult, *, age_context: bool) -> AgeGateDecision:
    if not age_context:
        return AgeGateDecision(age=None, is_18_plus=None, accepted=False)
    if result.is_18_plus is True:
        return AgeGateDecision(age=result.age or 18, is_18_plus=True, accepted=True)
    if result.is_18_plus is False:
        age = result.age if result.age is not None else 17
        return AgeGateDecision(
            age=age,
            is_18_plus=False,
            accepted=True,
            close_reason="underage" if age < 18 else None,
        )
    if result.age is not None:
        return AgeGateDecision(
            age=result.age,
            is_18_plus=result.age >= 18,
            accepted=True,
            close_reason="underage" if result.age < 18 else None,
        )
    return AgeGateDecision(age=None, is_18_plus=None, accepted=False)


async def controlled_context_cards(session, *, query: str | None = None, phase: str | None = None) -> list[dict[str, Any]]:
    result = await session.execute(
        select(KnowledgeCard)
        .where(
            KnowledgeCard.active.is_(True),
            KnowledgeCard.verification_status.in_(["approved", "verified"]),
        )
        .order_by(KnowledgeCard.created_at.asc())
        .limit(120)
    )
    cards = list(result.scalars().all())
    query_tokens = tokenize(query or "")
    phase_text = str(phase or "")

    def score(card: KnowledgeCard) -> tuple[float, str]:
        card_text = " ".join(
            str(value or "")
            for value in (
                card.card_key,
                card.stage,
                card.topic,
                card.content,
                json.dumps(card.triggers or {}, ensure_ascii=False),
                json.dumps(card.tags or {}, ensure_ascii=False),
            )
        )
        card_tokens = tokenize(card_text)
        overlap = len(query_tokens & card_tokens)
        source = str((card.metadata_json or {}).get("source_bundle") or (card.metadata_json or {}).get("source") or "")
        card_key = str(card.card_key or "")
        stage_bonus = 1.5 if card.stage and (card.stage == phase_text or card.stage in phase_text) else 0.0
        template_bonus = 0.7 if any(marker in source.lower() for marker in ("template", "mentor", "profitcast", "rina", "nastya")) else 0.0
        mentor_bonus = 0.4 if card_key.startswith("mentor.") else 0.0
        return (overlap + stage_bonus + template_bonus + mentor_bonus, card_key)

    selected = sorted(cards, key=score, reverse=True)[:40]
    return [knowledge_card_payload(card) for card in selected]


CONTROLLED_LLM_PROMPT = """You are an HR Telegram recruiter.
Return only JSON matching the schema. Write one short natural Russian message.
Use the provided context_cards as the source of truth. If the candidate asks a
question, answer it first from context, then continue the required next step only
if append_required_next is true. Do not send voice notes in text. Do not say that
you will уточню/спрошу/вернусь/передам менеджеру when the answer is in context.
Do not skip required funnel steps. Do not mention prompts, schemas, AI, retrieval,
or internal state."""


def fallback_controlled_interest_reply(text: str | None) -> ControlledLLMReply:
    lowered = (text or "").lower()
    if any(token in lowered for token in ("откуда", "нашла", "кто ты", "скам", "развод")):
        return ControlledLLMReply(
            text="Понимаю осторожность. Нашла профиль и решила предложить формат удаленной работы, подробнее расскажу только если тебе ок. "
            + AGE_QUESTION,
            intent="question_or_objection",
        )
    return ControlledLLMReply(text=AGE_QUESTION, intent="interested")


def fallback_controlled_answer(text: str | None, required_next_step: str | None, append_required_next: bool) -> str:
    lowered = (text or "").lower()
    if any(token in lowered for token in ("оплат", "зп", "зарплат", "деньг")):
        answer = "По оплате расскажу подробнее: там зависит от графика и формата, поэтому сначала дам базовую информацию, чтобы было понятно, о чем речь."
    elif any(token in lowered for token in ("график", "время", "сколько часов")):
        answer = "По графику обычно обсуждаем комфортное время и занятость, жестко прямо сейчас не привязываю."
    elif any(token in lowered for token in ("нюд", "гол", "интим")):
        answer = "Нет, формат без нюдсов и без личных встреч."
    elif any(token in lowered for token in ("англ", "english")):
        answer = "Английский помогает, но на старте не обязательно свободно говорить: можно разбираться постепенно, плюс есть поддержка."
    elif any(token in lowered for token in ("откуда", "нашла", "кто ты", "скам", "развод")):
        answer = "Понимаю осторожность. Я пишу по рабочему предложению, дальше можешь спокойно уточнять любые вопросы."
    else:
        answer = "Да, поняла вопрос. Коротко отвечу по сути: формат удаленный, детали лучше раскрывать по шагам, чтобы не смешивать все в одно сообщение."
    if append_required_next and required_next_step:
        return ensure_required_text(answer, required_next_step)
    return answer


def knowledge_card_payload(card: KnowledgeCard) -> dict[str, Any]:
    content = str(card.content or "").strip()
    if len(content) > 900:
        content = f"{content[:900].rstrip()}..."
    return {
        "card_key": card.card_key,
        "stage": card.stage,
        "topic": card.topic,
        "content": content,
        "triggers": card.triggers or {},
        "tags": card.tags or {},
        "metadata": card.metadata_json or {},
    }


def ensure_age_question(text: str | None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return AGE_QUESTION
    if age_question_already_present(cleaned):
        return cleaned
    separator = " " if cleaned.endswith((".", "!", "?")) else ". "
    return f"{cleaned}{separator}{AGE_QUESTION}"


def age_question_already_present(text: str) -> bool:
    lowered = text.lower()
    return "18" in lowered and any(token in lowered for token in ("есть", "уже", "исполни", "старше"))


def ensure_required_text(text: str, required_next_step: str) -> str:
    cleaned = (text or "").strip()
    required = (required_next_step or "").strip()
    if not required:
        return cleaned
    if required.lower() in cleaned.lower():
        return cleaned
    separator = " " if cleaned.endswith((".", "!", "?")) else ". "
    return f"{cleaned}{separator}{required}"


def has_candidate_question(text: str | None) -> bool:
    lowered = (text or "").lower()
    if "?" in lowered:
        return True
    markers = (
        "что",
        "как",
        "почему",
        "зачем",
        "сколько",
        "где",
        "когда",
        "какая",
        "какой",
        "откуда",
        "а если",
    )
    return any(marker in lowered for marker in markers)


async def maybe_answer_new_question_before_script(
    *,
    session,
    dialog: Dialog,
    phase: PhaseState,
    dry_run: bool,
    phase_name: str,
    required_next_step: str,
) -> bool:
    if phase.question_answer_message_id:
        sent_at = await message_sent_at(session, phase.question_answer_message_id)
        if sent_at is None:
            return True
        phase.question_answer_sent_at = sent_at.isoformat()
        phase.question_answer_message_id = None

    reply = await latest_inbound_after(session, dialog.id, parse_dt(phase.started_at))
    if reply is None or phase.last_processed_inbound_message_id == str(reply.id):
        return False
    if not has_candidate_question(reply.body):
        return False

    await cancel_pending_controlled_outbound(session, dialog.id, reason="candidate question before scripted step")
    phase.last_processed_inbound_message_id = str(reply.id)
    phase.last_voice_job_id = None
    message_id = None if dry_run else await enqueue_text(
        session,
        dialog,
        await controlled_llm_answer_text(
            session,
            dialog,
            reply,
            phase_name=phase_name,
            required_next_step=required_next_step,
            append_required_next=False,
        ),
        f"controlled:question_answer:{reply.id}",
        after=parse_dt(phase.started_at),
        reply_to_message_id=str(reply.id),
    )
    phase.question_answer_message_id = str(message_id) if message_id else None
    return True


async def cancel_pending_controlled_outbound(session, dialog_id, *, reason: str) -> int:
    result = await session.execute(
        select(OutboundJob).where(
            OutboundJob.dialog_id == dialog_id,
            OutboundJob.status.in_(["queued", "retry"]),
        )
    )
    cancelled = 0
    for job in result.scalars().all():
        job.status = "cancelled"
        job.next_attempt_at = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.error_message = reason
        if job.message_id:
            message = await session.get(Message, job.message_id)
            if message is not None:
                message.status = "cancelled"
        cancelled += 1
    await session.flush()
    return cancelled


async def enqueue_text(
    session,
    dialog: Dialog,
    text: str,
    topic: str,
    *,
    after: datetime | None,
    reply_to_message_id: str | None = None,
):
    existing = await latest_controlled_text_message(session, dialog, topic, after=after)
    if existing is not None:
        return existing.id
    now = datetime.now(UTC)
    message = Message(dialog_id=dialog.id, direction="outbound", sender_type="agent", body=text, status="scheduled")
    session.add(message)
    await session.flush()
    metadata = {"controlled_topic": topic}
    if reply_to_message_id:
        metadata["reply_to_message_id"] = str(reply_to_message_id)
    session.add(
        OutboundJob(
            account_id=dialog.account_id,
            dialog_id=dialog.id,
            message_id=message.id,
            target_username=dialog.telegram_username,
            peer=build_peer_from_dialog(dialog),
            text=text,
            media_metadata=metadata,
            status="queued",
            scheduled_at=now,
            next_attempt_at=now,
        )
    )
    await session.flush()
    return message.id


async def enqueue_voice(
    session,
    dialog: Dialog,
    base_dir: Path,
    item: dict[str, Any],
    *,
    after: datetime | None,
    pack_name: str | None = None,
    recording_delay_seconds: float | None = None,
    reply_to_message_id: str | None = None,
):
    existing = await latest_controlled_voice_job(session, dialog, int(item.get("order") or 0), after=after)
    if existing is not None:
        return existing.id
    now = datetime.now(UTC)
    recording_wait = recording_delay_seconds if recording_delay_seconds is not None else item.get("recording_delay_seconds") or DEFAULT_VOICE_RECORDING_SECONDS
    scheduled_at = now + timedelta(seconds=max(0.0, float(recording_wait or 0.0)))
    media_path = base_dir / str(item["file"])
    message = Message(
        dialog_id=dialog.id,
        direction="outbound",
        sender_type="agent",
        body=f"[voice] {item.get('topic') or item['file']}",
        status="scheduled",
    )
    session.add(message)
    await session.flush()
    job = OutboundJob(
        account_id=dialog.account_id,
        dialog_id=dialog.id,
        message_id=message.id,
        target_username=dialog.telegram_username,
        peer=build_peer_from_dialog(dialog),
        job_type="voice",
        text=message.body,
        media_path=str(media_path),
        media_mime_type="audio/ogg",
        media_metadata={
            "caption": item.get("caption") or "",
            "duration_seconds": item.get("duration_seconds") or 0,
            "recording_wait_seconds": recording_wait,
            "recording_delay_seconds": 0,
            "voice_intro_order": item.get("order"),
            "voice_pack": pack_name,
            "stage_hint": item.get("stage_hint"),
            "topic": item.get("topic"),
            "related_cards": item.get("related_cards") or [],
            "reply_to_message_id": str(reply_to_message_id) if reply_to_message_id else None,
        },
        typing_action="sendMessageRecordAudioAction",
        status="queued",
        scheduled_at=scheduled_at,
        next_attempt_at=scheduled_at,
    )
    session.add(job)
    await session.flush()
    return job.id


async def prepare_age_gate(session, dialog: Dialog) -> None:
    lead = await get_or_create_lead(session, dialog)
    manager = StateManager(session)
    state = await manager.get_or_create_state(lead)
    await manager.create_agenda_for_interested_lead(lead)
    state.stage = "age_gate"
    state.current_goal = "Confirm candidate is 18+"
    state.open_loop = {"item_key": "age.confirm_18", "question": AGE_QUESTION}
    await session.execute(
        update(LeadAgendaItem)
        .where(LeadAgendaItem.lead_id == lead.id, LeadAgendaItem.item_key.like("trust.%"))
        .values(status="done")
    )
    await session.execute(
        update(LeadAgendaItem)
        .where(LeadAgendaItem.lead_id == lead.id, LeadAgendaItem.item_key == "age.confirm_18")
        .values(status="active")
    )
    await session.flush()


async def prepare_basic_info_pack(session, dialog: Dialog) -> None:
    lead = await get_or_create_lead(session, dialog)
    state = await StateManager(session).get_or_create_state(lead)
    state.stage = "basic_info_pack"
    state.current_goal = "Send basic voice info pack and check if candidate wants salary/schedule details"
    state.open_loop = None
    await session.execute(
        update(LeadAgendaItem)
        .where(LeadAgendaItem.lead_id == lead.id, LeadAgendaItem.item_key == "age.confirm_18")
        .values(status="done")
    )
    await session.flush()


async def prepare_deep_info_pack(session, dialog: Dialog) -> None:
    lead = await get_or_create_lead(session, dialog)
    state = await StateManager(session).get_or_create_state(lead)
    state.stage = "qualification_faq"
    state.current_goal = "Send deeper info pack about salary, schedule, format, and next questions"
    state.open_loop = None
    await session.flush()


async def prepare_qualification_faq(session, dialog: Dialog) -> None:
    lead = await get_or_create_lead(session, dialog)
    state = await StateManager(session).get_or_create_state(lead)
    state.stage = "qualification_faq"
    state.current_goal = "Answer candidate questions with RAG, then resume company and personalization agenda"
    state.open_loop = None
    await session.flush()


async def apply_profile_slot(session, dialog: Dialog, slots: dict[str, Any]) -> None:
    lead = await get_or_create_lead(session, dialog)
    await StateManager(session).apply_slot_patch(lead, slots, source="controlled_funnel", confidence=1.0)


async def close_controlled_lead(session, dialog: Dialog, reason: str) -> None:
    lead = await get_or_create_lead(session, dialog)
    state = await StateManager(session).get_or_create_state(lead)
    state.stage = "closed"
    state.status = "closed"
    lead.lost_reason = reason
    await session.flush()


async def reset_dialog_run(session, dialog: Dialog) -> None:
    await session.execute(
        update(OutboundJob)
        .where(OutboundJob.dialog_id == dialog.id, OutboundJob.status.in_(["queued", "retry", "processing"]))
        .values(status="cancelled", error_message="controlled funnel reset")
    )
    await session.execute(
        update(InboundEvent)
        .where(InboundEvent.dialog_id == dialog.id, InboundEvent.status.in_(["queued", "retry"]))
        .values(status="processed", error_message="controlled funnel reset", processed_at=datetime.now(UTC))
    )
    lead = await get_or_create_lead(session, dialog)
    await session.execute(delete(LeadProfileSlot).where(LeadProfileSlot.lead_id == lead.id))
    await session.execute(delete(LeadAgendaItem).where(LeadAgendaItem.lead_id == lead.id))
    await session.execute(delete(LeadBrainState).where(LeadBrainState.lead_id == lead.id))
    lead.qualification_status = "new"
    lead.interest_status = None
    lead.lost_reason = None
    state = await StateManager(session).get_or_create_state(lead)
    state.metadata_json = {
        **dict(state.metadata_json or {}),
        CONTROL_KEY: asdict(PhaseState(phase="new", started_at=datetime.now(UTC).isoformat())),
    }
    await session.flush()


async def find_target_dialog(session, username: str) -> Dialog | None:
    normalized = username.strip().lower().lstrip("@")
    result = await session.execute(
        select(Dialog)
        .where(
            func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized,
            Dialog.telegram_peer_type.is_not(None),
            Dialog.telegram_peer_id.is_not(None),
        )
        .order_by(Dialog.updated_at.desc(), Dialog.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_or_create_lead(session, dialog: Dialog) -> Lead:
    result = await session.execute(select(Lead).where(Lead.dialog_id == dialog.id).limit(1))
    lead = result.scalar_one_or_none()
    if lead is not None:
        return lead
    lead = Lead(dialog_id=dialog.id, qualification_status="new")
    session.add(lead)
    await session.flush()
    return lead


async def load_or_create_phase(session, dialog: Dialog) -> PhaseState:
    state = await controlled_state(session, dialog)
    raw = (state.metadata_json or {}).get(CONTROL_KEY)
    if isinstance(raw, dict) and raw.get("phase"):
        defaults = asdict(PhaseState(phase="new", started_at=datetime.now(UTC).isoformat()))
        return PhaseState(**{**defaults, **raw})
    return PhaseState(phase="new", started_at=datetime.now(UTC).isoformat())


async def store_phase(session, dialog: Dialog, phase: PhaseState) -> None:
    state = await controlled_state(session, dialog)
    metadata = dict(state.metadata_json or {})
    metadata[CONTROL_KEY] = asdict(phase)
    state.metadata_json = metadata
    await session.flush()


async def controlled_state(session, dialog: Dialog) -> LeadBrainState:
    lead = await get_or_create_lead(session, dialog)
    return await StateManager(session).get_or_create_state(lead)


async def latest_controlled_text_message(
    session,
    dialog: Dialog,
    topic: str,
    *,
    after: datetime | None,
) -> Message | None:
    created_filter = [Message.created_at > after] if after is not None else []
    result = await session.execute(
        select(Message)
        .join(OutboundJob, OutboundJob.message_id == Message.id)
        .where(
            Message.dialog_id == dialog.id,
            OutboundJob.dialog_id == dialog.id,
            OutboundJob.media_metadata.op("->>")("controlled_topic") == topic,
            OutboundJob.status.notin_(["cancelled", "failed", "dead_letter", "blocked"]),
            *created_filter,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    candidate = result.scalar_one_or_none()
    if candidate is not None:
        return candidate
    if topic != "controlled:first_touch":
        return None
    result = await session.execute(
        select(Message)
        .where(
            Message.dialog_id == dialog.id,
            Message.direction == "outbound",
            Message.body == await greeting_text(session),
            *created_filter,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_controlled_voice_job(session, dialog: Dialog, order: int, *, after: datetime | None) -> OutboundJob | None:
    created_filter = [OutboundJob.created_at > after] if after is not None else []
    result = await session.execute(
        select(OutboundJob)
        .where(
            OutboundJob.dialog_id == dialog.id,
            OutboundJob.job_type == "voice",
            OutboundJob.media_metadata.op("->>")("voice_intro_order") == str(order),
            OutboundJob.status.notin_(["cancelled", "failed", "dead_letter", "blocked"]),
            *created_filter,
        )
        .order_by(OutboundJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def acquire_advisory_lock(connection, key: str) -> bool:
    result = await connection.execute(
        text("select pg_try_advisory_lock(hashtext(:lock_key))"),
        {"lock_key": key},
    )
    return bool(result.scalar_one())


async def release_advisory_lock(connection, key: str) -> None:
    await connection.execute(
        text("select pg_advisory_unlock(hashtext(:lock_key))"),
        {"lock_key": key},
    )


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


async def latest_inbound_after(session, dialog_id, after: datetime | None) -> Message | None:
    query = select(Message).where(Message.dialog_id == dialog_id, Message.direction == "inbound")
    if after is not None:
        query = query.where(Message.created_at > after)
    result = await session.execute(query.order_by(Message.created_at.desc()).limit(1))
    return result.scalar_one_or_none()


async def candidate_spoke_after(session, dialog_id, after: datetime | None) -> bool:
    if after is None:
        return False
    result = await session.execute(
        select(Message.id)
        .where(Message.dialog_id == dialog_id, Message.direction == "inbound", Message.created_at > after)
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def mark_inbound_event_processed(session, message_id, reason: str) -> None:
    await session.execute(
        update(InboundEvent)
        .where(
            InboundEvent.status.in_(["queued", "retry"]),
            InboundEvent.payload.op("->>")("db_message_id") == str(message_id),
        )
        .values(status="processed", error_message=reason, processed_at=datetime.now(UTC))
    )


async def message_sent_at(session, message_id: str | None) -> datetime | None:
    if not message_id:
        return None
    message = await session.get(Message, message_id)
    return message.sent_at or message.updated_at if message and message.status == "sent" else None


async def message_created_at(session, message_id: str | None) -> datetime | None:
    if not message_id:
        return None
    message = await session.get(Message, message_id)
    return message.created_at if message else None


async def outbound_job_status(session, job_id: str) -> tuple[str | None, datetime | None]:
    job = await session.get(OutboundJob, job_id)
    if job is None:
        return None, None
    return job.status, job.sent_at


async def outbound_message_job_status(session, message_id: str) -> str | None:
    result = await session.execute(
        select(OutboundJob.status)
        .where(OutboundJob.message_id == message_id)
        .order_by(OutboundJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_terminal_state(session, dialog: Dialog) -> str | None:
    result = await session.execute(
        select(LeadBrainState.stage).where(LeadBrainState.dialog_id == dialog.id).order_by(LeadBrainState.updated_at.desc()).limit(1)
    )
    stage = result.scalar_one_or_none()
    if stage in {"closed", "handoff"}:
        return stage
    result = await session.execute(
        select(BrainRun.status).where(BrainRun.dialog_id == dialog.id).order_by(BrainRun.created_at.desc()).limit(1)
    )
    return None


async def set_fast_pacing(session, seconds: int) -> None:
    await session.execute(
        update(Account).values(
            send_interval_seconds=max(0, seconds),
            send_jitter_seconds=0,
            next_available_at=datetime.now(UTC),
        )
    )
    await session.flush()


def is_refusal(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("не интересно", "неинтересно", "не надо", "отстан", "нет спасибо"))


def is_refusal(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(token in lowered for token in ("не интересно", "неинтересно", "не надо", "отстан", "не пиши", "нет спасибо"))


def is_interest(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(
        token in lowered
        for token in (
            "да",
            "интересно",
            "расскажи",
            "послушаю",
            "слушаю",
            "давай",
            "ок",
            "го",
            "что за работа",
            "какая работа",
        )
    )


def extract_age(text: str | None) -> int | None:
    import re

    lowered = (text or "").lower()
    if "нет 18" in lowered or "меньше 18" in lowered or "нету 18" in lowered:
        return 17
    match = re.search(r"\b(1[0-7]|18|19|[2-5][0-9])\b", lowered)
    if match:
        return int(match.group(1))
    if any(token in lowered for token in ("есть 18", "уже 18", "18+", "совершеннолет")):
        return 18
    return None


def is_refusal(text: str | None) -> bool:
    lowered = (text or "").lower()
    tokens = (
        "\u043d\u0435 \u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u043e",
        "\u043d\u0435\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u043e",
        "\u043d\u0435 \u043d\u0430\u0434\u043e",
        "\u043e\u0442\u0441\u0442\u0430\u043d",
        "\u043d\u0435 \u043f\u0438\u0448\u0438",
        "\u043d\u0435\u0442 \u0441\u043f\u0430\u0441\u0438\u0431\u043e",
    )
    return any(token in lowered for token in tokens)


def is_interest(text: str | None) -> bool:
    lowered = (text or "").lower()
    tokens = (
        "\u0434\u0430",
        "\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u043e",
        "\u0440\u0430\u0441\u0441\u043a\u0430\u0436\u0438",
        "\u043f\u043e\u0441\u043b\u0443\u0448\u0430\u044e",
        "\u0441\u043b\u0443\u0448\u0430\u044e",
        "\u0434\u0430\u0432\u0430\u0439",
        "\u043e\u043a",
        "\u0433\u043e",
        "\u0447\u0442\u043e \u0437\u0430 \u0440\u0430\u0431\u043e\u0442\u0430",
        "\u043a\u0430\u043a\u0430\u044f \u0440\u0430\u0431\u043e\u0442\u0430",
    )
    return any(token in lowered for token in tokens)


def extract_age(text: str | None) -> int | None:
    import re

    lowered = (text or "").lower()
    if any(token in lowered for token in ("\u043d\u0435\u0442 18", "\u043d\u0435\u0442\u0443 18", "\u043d\u0435 \u0435\u0441\u0442\u044c 18", "\u043c\u0435\u043d\u044c\u0448\u0435 18")):
        return 17
    match = re.search(r"\b(1[0-7]|18|19|[2-5][0-9])\b", lowered)
    if match:
        return int(match.group(1))
    if any(token in lowered for token in ("\u0435\u0441\u0442\u044c 18", "\u0443\u0436\u0435 18", "18+", "\u0441\u043e\u0432\u0435\u0440\u0448\u0435\u043d\u043d\u043e\u043b\u0435\u0442")):
        return 18
    return None


def classify_age_reply(text: str | None, *, age_context: bool) -> AgeGateDecision:
    import re

    lowered = " ".join((text or "").lower().replace("ё", "е").split())
    if not lowered:
        return AgeGateDecision(age=None, is_18_plus=None, accepted=False)
    words = set(re.findall(r"[а-яa-z0-9+]+", lowered))
    negative_phrases = (
        "нет 18",
        "нету 18",
        "нет еще 18",
        "не есть 18",
        "меньше 18",
        "не исполнилось 18",
    )
    if age_context and any(token in lowered for token in negative_phrases):
        return AgeGateDecision(age=17, is_18_plus=False, accepted=True, close_reason="underage")
    age = extract_age(lowered)
    if age is not None:
        return AgeGateDecision(
            age=age,
            is_18_plus=age >= 18,
            accepted=True,
            close_reason="underage" if age < 18 else None,
        )
    if not age_context:
        return AgeGateDecision(age=None, is_18_plus=None, accepted=False)
    if words & {"нет", "неа", "net", "no"}:
        return AgeGateDecision(age=17, is_18_plus=False, accepted=True, close_reason="underage")
    positive_phrases = (
        "да",
        "да есть",
        "уже есть",
        "мне уже есть",
        "конечно",
        "ага",
        "угу",
        "есть уже",
        "исполн",
        "совершеннолет",
    )
    if (
        words & {"да", "ага", "угу", "есть", "конечно", "da", "yes", "yep"}
        or any(token in lowered for token in positive_phrases if token not in {"да", "ага", "угу", "конечно"})
    ):
        return AgeGateDecision(age=18, is_18_plus=True, accepted=True)
    return AgeGateDecision(age=None, is_18_plus=None, accepted=False)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_inbound(result) -> str:
    if result is None:
        return "-"
    return f"p:{result.processed},r:{result.retry},f:{result.failed}"


def format_outbound(result) -> str:
    if result is None:
        return "-"
    return f"s:{result.sent},c:{result.cancelled},r:{result.retry},f:{result.failed}"


if __name__ == "__main__":
    asyncio.run(main())
