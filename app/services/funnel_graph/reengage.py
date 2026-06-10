from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.dialog import Dialog
from app.models.funnel_graph import LeadFunnelRuntime
from app.models.lead import Lead
from app.models.message import Message
from app.models.outbound_job import OutboundJob
from app.services.funnel_graph.actions import FunnelActionExecutor
from app.services.funnel_graph.funnel_policy import TERMINAL_STAGES, get_stage_policy
from app.services.funnel_graph.reply import question_variants

logger = logging.getLogger(__name__)

# Стадии, на которых бампить НЕЛЬЗЯ: терминальные + осознанная пауза до 18-летия
# (там уже запланированы отложенные сообщения на день рождения).
EXCLUDED_STAGES: frozenset[str] = frozenset(TERMINAL_STAGES | {"scheduled_until_18"})

# «Холодные» лиды: получили опенер и ни разу не ответили. Тексты по касаниям.
# Все варианты согласованы с FAQ (не вебкам, можно с телефона, гибкий график);
# конкретные цифры дохода намеренно не называем — политика «цифры на собесе».
COLD_TOUCH_TEXTS: tuple[tuple[str, ...], ...] = (
    (
        "слушай, понимаю, что сообщение от незнакомки выглядит подозрительно)) если коротко: это разговорные стримы, не вебкам и не оф, можно прямо с телефона. если хоть чуть интересно — просто напиши «расскажи»",
        "не пугайся, я не спам-бот)) реально ищу девочек в команду на разговорные стримы. без вебкама и без вложений. если интересно — напиши, всё расскажу голосом)",
    ),
    (
        "ещё разок напомню о себе) у нас можно спокойно совмещать с учёбой или работой, график подстраиваем. если есть сомнения или вопросы — задай любой, отвечу честно)",
        "если смущает, что это «что-то мутное» — понимаю) но это обычные разговорные эфиры, не вебкам и не онлифанс. могу скинуть голосовые с подробностями, только скажи)",
    ),
    (
        "ладно, не буду надоедать)) если вдруг надумаешь узнать, что за работа — просто напиши мне, я на связи 🐬",
        "всё, больше не дёргаю) если станет любопытно — пиши в любое время, расскажу и покажу, как всё устроено)",
    ),
)

# «Тёплые» лиды: переписка была, девочка затихла. Первые касания возвращают
# к вопросу текущей стадии (переформулировка), финальное — мягкое прощание.
WARM_PING_PREFIXES: tuple[str, ...] = (
    "эй, ты куда пропала?)",
    "напомню о себе))",
)
WARM_FINAL_TEXTS: tuple[str, ...] = (
    "не буду больше писать, чтобы не надоедать) если захочешь продолжить — просто ответь на это сообщение, я тут 🐬",
    "окей, не дёргаю больше) если надумаешь — напиши в любой момент, продолжим с того же места)",
)
# Стадии без канонного вопроса — общий мягкий возврат к диалогу.
WARM_GENERIC_QUESTION = "как ты смотришь на то, чтобы продолжить? могу рассказать дальше)"


@dataclass(slots=True, frozen=True)
class ReengageCandidate:
    runtime: LeadFunnelRuntime
    dialog: Dialog
    last_direction: str
    last_message_at: datetime
    has_inbound: bool


def parse_touch_delays(raw: str) -> list[timedelta]:
    delays: list[timedelta] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            delays.append(timedelta(hours=float(part)))
        except ValueError:
            continue
    return delays or [timedelta(hours=4), timedelta(hours=20), timedelta(hours=48)]


def next_touch_number(
    *,
    touch_count: int,
    last_message_at: datetime,
    now: datetime,
    delays: list[timedelta],
) -> int | None:
    """Номер следующего касания (1-based), если пришло его время; иначе None.

    Пауза отсчитывается от ПОСЛЕДНЕГО нашего сообщения (включая прошлый бамп),
    поэтому лесенка нарастает сама: 4ч после опенера, 20ч после бампа №1, 48ч
    после бампа №2. После len(delays) касаний — навсегда тишина."""
    if touch_count >= len(delays):
        return None
    if last_message_at.tzinfo is None:
        last_message_at = last_message_at.replace(tzinfo=UTC)
    if now < last_message_at + delays[touch_count]:
        return None
    return touch_count + 1


def touch_text(
    *,
    touch_number: int,
    cold: bool,
    stage: str,
    dialog_key: str,
    touch_count_total: int,
) -> str:
    """Детерминированный текст касания: разные лиды получают разные варианты
    (ротация по hash диалога), один лид никогда не получает один текст дважды
    (вариант сдвигается номером касания)."""
    seed = int(hashlib.sha1(dialog_key.encode("utf-8")).hexdigest(), 16)
    if cold:
        pool = COLD_TOUCH_TEXTS[min(touch_number, len(COLD_TOUCH_TEXTS)) - 1]
        return pool[(seed + touch_number) % len(pool)]
    if touch_number >= touch_count_total:
        return WARM_FINAL_TEXTS[seed % len(WARM_FINAL_TEXTS)]
    question = get_stage_policy(stage).current_question
    if not question:
        question = WARM_GENERIC_QUESTION
    variants = question_variants(question)
    variant = variants[(seed + touch_number) % len(variants)]
    prefix = WARM_PING_PREFIXES[(seed + touch_number) % len(WARM_PING_PREFIXES)]
    return f"{prefix} {variant}"


class ReengagementService:
    """Догоняем замолчавших лидов: если последнее сообщение в диалоге — НАШЕ и
    лид молчит дольше порога, шлём «бамп» (до 3 раз, потом замолкаем навсегда).

    До этого сервиса замолчавший лид не получал НИ ОДНОГО повторного касания —
    29 из 50 живых лидов умерли именно так (см. docs/CHANGELOG_AGENT.md)."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def run_once(self, *, account_id: str | None = None, limit: int | None = None) -> int:
        if not self.settings.reengage_enabled:
            return 0
        delays = parse_touch_delays(self.settings.reengage_touch_delays_hours)
        batch_limit = max(1, limit if limit is not None else self.settings.reengage_batch_limit)
        now = datetime.now(UTC)
        sent = 0
        for candidate in await self._load_candidates(account_id):
            if sent >= batch_limit:
                break
            if candidate.last_direction != "outbound":
                continue  # ход за воронкой (она ещё не ответила лиду) — не лезем
            metadata = dict(candidate.runtime.metadata_json or {})
            touch_count = int(metadata.get("reengage_count") or 0)
            touch_number = next_touch_number(
                touch_count=touch_count,
                last_message_at=candidate.last_message_at,
                now=now,
                delays=delays,
            )
            if touch_number is None:
                continue
            text = touch_text(
                touch_number=touch_number,
                cold=not candidate.has_inbound,
                stage=str(candidate.runtime.stage or "interest_check"),
                dialog_key=str(candidate.dialog.id),
                touch_count_total=len(delays),
            )
            await self._send_touch(candidate, touch_number, text)
            candidate.runtime.metadata_json = {
                **metadata,
                "reengage_count": touch_number,
                "reengage_last_at": now.isoformat(),
            }
            sent += 1
            logger.info(
                "reengage touch sent",
                extra={
                    "dialog_id": str(candidate.dialog.id),
                    "touch": touch_number,
                    "cold": not candidate.has_inbound,
                    "stage": candidate.runtime.stage,
                },
            )
        if sent:
            await self.session.commit()
        return sent

    async def _send_touch(self, candidate: ReengageCandidate, touch_number: int, text: str) -> None:
        lead = await self.session.get(Lead, candidate.runtime.lead_id)
        if lead is None:
            return
        await FunnelActionExecutor(self.session).execute(
            dialog=candidate.dialog,
            lead=lead,
            runtime=candidate.runtime,
            actions=[
                {
                    "type": "send_text",
                    "text": text,
                    "delay_seconds": 0,
                    # Идемпотентность: одно касание №N на диалог, что бы ни случилось
                    # с процессом между скриптами/циклами.
                    "idempotency_key": f"reengage:{candidate.dialog.id}:{touch_number}",
                }
            ],
        )

    async def _load_candidates(self, account_id: str | None) -> list[ReengageCandidate]:
        filters = [
            LeadFunnelRuntime.stage.notin_(list(EXCLUDED_STAGES)),
            Dialog.status != "uncontactable",
        ]
        if account_id:
            filters.append(Dialog.account_id == UUID(str(account_id)))
        rows = (
            await self.session.execute(
                select(LeadFunnelRuntime, Dialog)
                .join(Dialog, Dialog.id == LeadFunnelRuntime.dialog_id)
                .where(*filters)
            )
        ).all()
        if not rows:
            return []
        dialog_ids = [dialog.id for _, dialog in rows]

        # Последнее сообщение каждого диалога (postgres DISTINCT ON).
        at_expr = func.coalesce(Message.sent_at, Message.created_at)
        last_rows = (
            await self.session.execute(
                select(Message.dialog_id, Message.direction, at_expr)
                .where(Message.dialog_id.in_(dialog_ids))
                .distinct(Message.dialog_id)
                .order_by(Message.dialog_id, at_expr.desc())
            )
        ).all()
        last_by_dialog = {row[0]: (row[1], row[2]) for row in last_rows}

        inbound_rows = (
            await self.session.execute(
                select(Message.dialog_id)
                .where(Message.dialog_id.in_(dialog_ids), Message.direction == "inbound")
                .group_by(Message.dialog_id)
            )
        ).all()
        has_inbound = {row[0] for row in inbound_rows}

        # Диалоги, где уже что-то ждёт отправки, не бампим — сообщение и так уйдёт.
        pending_rows = (
            await self.session.execute(
                select(OutboundJob.dialog_id)
                .where(
                    OutboundJob.dialog_id.in_(dialog_ids),
                    OutboundJob.status.in_(("queued", "retry", "processing")),
                )
                .group_by(OutboundJob.dialog_id)
            )
        ).all()
        pending = {row[0] for row in pending_rows}

        candidates: list[ReengageCandidate] = []
        for runtime, dialog in rows:
            if dialog.id in pending or dialog.id not in last_by_dialog:
                continue
            direction, last_at = last_by_dialog[dialog.id]
            candidates.append(
                ReengageCandidate(
                    runtime=runtime,
                    dialog=dialog,
                    last_direction=str(direction or ""),
                    last_message_at=last_at,
                    has_inbound=dialog.id in has_inbound,
                )
            )
        return candidates
