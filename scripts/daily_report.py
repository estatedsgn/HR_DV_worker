"""Ежедневная сводка по воронке в управляющий чат + переписки файлами.

Запускается systemd-таймером раз в сутки (hrdv-daily-report.timer), обычно
вечером после закрытия рабочего окна. Делает две вещи:

  1. Считает воронку и шлёт сводку текстом в Telegram-чат супервизора:
     касания → ответили (интерес) → прошли дальше → дошли до назначения,
     всего и по аккаунтам (Юля / Марго), + что прибавилось за сегодня.

  2. Экспортирует дневные переписки (scripts/export_chats) и присылает каждый
     txt-файл документом в тот же чат — чтобы их можно было читать и разбирать.

Определения метрик (специально простые и считаемые; правим по ходу):
  • Касания          — лидов, которым ушёл опенер (есть запись в воронке).
  • Ответили/интерес — лидов с ≥1 входящим сообщением (откликнулись на опенер).
  • Прошли дальше    — текущая стадия ≥ age_check (прошли проверку интереса,
                       пошли по квалификации). Снимок по текущей стадии.
  • До назначения    — дошли до собеседования/передачи (human_handoff) либо
                       стадии ≥ interview_offer.
  • Потеряно         — стадия lost / do_not_contact.

Никогда не падает ненулевым кодом (таймер не должен спамить ошибками).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent

# Порядок стадий воронки (мельче индекс — раньше). Точный порядок середины не
# критичен: пороги вех берём по якорным стадиям ниже.
STAGE_ORDER = [
    "new",
    "inbound_warmup",
    "interest_check",
    "try_interest_check",
    "age_check",
    "age_pending_18",
    "scheduled_until_18",
    "work_intro_delivery",
    "salary_schedule_offer",
    "salary_schedule_delivery",
    "post_equipment_questions_check",
    "profile_theme_check",
    "support_smalltalk",
    "room_available_check",
    "equipment_phone_check",
    "interview_offer",
    "contact_collection",
    "interview_day_check",
    "interview_time_check",
    "interview_custom_time",
    "human_handoff",
    "ready_for_interview",
]
_RANK = {s: i for i, s in enumerate(STAGE_ORDER)}
LOST_STAGES = {"lost", "do_not_contact"}
RANK_FURTHER = _RANK["age_check"]       # «прошли дальше» — с этой стадии
RANK_APPOINTMENT = _RANK["interview_offer"]  # «до назначения» — с этой стадии


def _rank(stage: str) -> int:
    return _RANK.get(stage, -1)


class Bucket:
    """Счётчики воронки для одной группы (аккаунт или всего)."""

    def __init__(self) -> None:
        self.touches = 0
        self.replied = 0
        self.further = 0
        self.appointment = 0
        self.lost = 0
        self.new_today = 0
        self.stages: dict[str, int] = {}

    def add(self, stage: str, has_inbound: bool, is_today: bool, handoff: bool) -> None:
        self.touches += 1
        self.stages[stage] = self.stages.get(stage, 0) + 1
        if has_inbound:
            self.replied += 1
        if stage in LOST_STAGES:
            self.lost += 1
        if _rank(stage) >= RANK_FURTHER:
            self.further += 1
        if handoff or _rank(stage) >= RANK_APPOINTMENT:
            self.appointment += 1
        if is_today:
            self.new_today += 1


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "—"


def _bucket_lines(b: Bucket) -> list[str]:
    return [
        f"👋 Касания: {b.touches}",
        f"💬 Ответили (интерес): {b.replied} ({_pct(b.replied, b.touches)})",
        f"➡️ Прошли дальше: {b.further} ({_pct(b.further, b.touches)})",
        f"📅 До назначения: {b.appointment} ({_pct(b.appointment, b.touches)})",
        f"❌ Потеряно: {b.lost}",
    ]


async def _compute(report_date) -> dict:
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    settings = get_settings()
    total = Bucket()
    per_account: dict[str, Bucket] = {}

    async with AsyncSessionLocal() as s:
        # Пред-сидим все аккаунты, чтобы пустые (напр. только заведённый #2)
        # тоже попадали в отчёт нулями, а не молча выпадали.
        accs = (await s.execute(text(
            "select id, crmchat_account_id from accounts"
        ))).all()
        for acc_id, crmchat_id in accs:
            label = settings.label_for_account(str(acc_id), crmchat_id) or (crmchat_id or str(acc_id))
            per_account.setdefault(label, Bucket())

        # Лиды с воронкой + сигнал «ответил» + дата захвата + аккаунт.
        rows = (
            await s.execute(
                text(
                    """
                    select a.id acc_id, a.crmchat_account_id crmchat_id,
                           lfr.stage, lfr.created_at,
                           exists(
                               select 1 from messages m
                               where m.dialog_id = lfr.dialog_id
                                 and m.direction = 'inbound'
                           ) has_inbound,
                           exists(
                               select 1 from human_handoffs hh
                               where hh.dialog_id = lfr.dialog_id
                           ) handoff
                    from lead_funnel_runtime lfr
                    join dialogs d on d.id = lfr.dialog_id
                    join accounts a on a.id = d.account_id
                    """
                )
            )
        ).all()

    for acc_id, crmchat_id, stage, created_at, has_inbound, handoff in rows:
        label = settings.label_for_account(str(acc_id), crmchat_id) or (crmchat_id or str(acc_id))
        is_today = created_at is not None and created_at.date() == report_date
        b = per_account.setdefault(label, Bucket())
        b.add(stage, bool(has_inbound), is_today, bool(handoff))
        total.add(stage, bool(has_inbound), is_today, bool(handoff))

    return {"total": total, "per_account": per_account}


def _stage_breakdown(b: Bucket) -> str:
    ordered = sorted(
        b.stages.items(),
        key=lambda kv: (_rank(kv[0]) if _rank(kv[0]) >= 0 else 999, kv[0]),
    )
    return ", ".join(f"{st} {n}" for st, n in ordered)


def build_total_message(report_date_label: str, data: dict) -> str:
    """Короткая шапка-тотал по всем аккаунтам."""
    total: Bucket = data["total"]
    per_account: dict[str, Bucket] = data["per_account"]
    lines = [f"📊 Воронка — {report_date_label} (всего по {len(per_account)} акк.)", ""]
    lines += _bucket_lines(total)
    if total.new_today:
        lines.append(f"🆕 новых касаний за сегодня: +{total.new_today}")
    lines.append("")
    lines.append("ℹ️ ответили = откликнулись на опенер; дальше = прошли проверку "
                 "интереса; назначение = дошли до собеседования/передачи.")
    lines.append("↓ детально по аккаунтам — ниже, с переписками файлом.")
    return "\n".join(lines)


def build_account_message(label: str, b: Bucket, report_date_label: str) -> str:
    """Полный отчёт по одному аккаунту."""
    lines = [f"👤 {label} — {report_date_label}", ""]
    lines += _bucket_lines(b)
    if b.new_today:
        lines.append(f"🆕 новых касаний за сегодня: +{b.new_today}")
    if b.stages:
        lines.append("")
        lines.append("Стадии: " + _stage_breakdown(b))
    return "\n".join(lines)


def _send_message(token: str, chat_id: str, text: str) -> None:
    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    resp.raise_for_status()


def _send_document(token: str, chat_id: str, path: Path, caption: str | None = None) -> None:
    with path.open("rb") as fh:
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendDocument",
            data=data,
            files={"document": (path.name, fh, "text/plain")},
            timeout=120,
        )
    resp.raise_for_status()


def _safe_label(label: str) -> str:
    """Имя txt-файла переписок для метки — как в export_chats."""
    return "".join(c for c in label if c.isalnum() or c in "-_ ").strip() or "account"


async def _export_transcripts(days: int) -> Path | None:
    """Экспортирует переписки за день и возвращает каталог с txt-файлами."""
    try:
        try:
            # При запуске файлом (systemd) на пути лежит каталог scripts/.
            from export_chats import OUT_DIR, _export_once
        except ImportError:
            # При запуске как модуль из корня (python -m scripts.daily_report).
            from scripts.export_chats import OUT_DIR, _export_once

        await _export_once(days)
        from datetime import UTC

        day_dir = OUT_DIR / datetime.now(UTC).date().isoformat()
        return day_dir if day_dir.exists() else None
    except Exception as exc:  # noqa: BLE001
        print(f"daily_report: экспорт переписок не удался: {exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Ежедневная сводка воронки + переписки")
    parser.add_argument("--no-transcripts", action="store_true",
                        help="Не прикладывать файлы переписок (только сводка).")
    parser.add_argument("--days", type=int, default=1,
                        help="За сколько суток собирать переписки (по умолч. 1).")
    args = parser.parse_args()

    s = get_settings()
    token = s.control_bot_token
    chat_id = s.control_admin_chat_id
    if not token or not chat_id:
        print("daily_report: CONTROL_BOT_TOKEN/CONTROL_ADMIN_CHAT_ID не заданы — пропуск")
        return 0

    tz = ZoneInfo(s.daivinchik_timezone)
    now = datetime.now(tz)
    report_date = now.date()
    date_label = now.strftime("%d.%m.%Y")

    # Вся async-работа в ОДНОМ event loop: глобальный engine держит пул на
    # первом loop, поэтому второй asyncio.run() упал бы «different loop».
    async def _gather():
        data = await _compute(report_date)
        day_dir = None if args.no_transcripts else await _export_transcripts(args.days)
        return data, day_dir

    try:
        data, day_dir = asyncio.run(_gather())
    except Exception as exc:  # noqa: BLE001
        try:
            _send_message(token, chat_id,
                          f"📊 Воронка — {date_label}\n‼️ не удалось посчитать: "
                          f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return 0

    # 1) Короткий тотал.
    try:
        _send_message(token, chat_id, build_total_message(date_label, data))
        print("daily_report: тотал отправлен")
    except Exception as exc:  # noqa: BLE001
        print(f"daily_report: тотал не отправлен: {exc}")

    # 2) По каждому аккаунту: отчёт + его переписки файлом.
    for label, bucket in sorted(data["per_account"].items()):
        try:
            _send_message(token, chat_id, build_account_message(label, bucket, date_label))
            print(f"daily_report: отчёт по {label} отправлен")
        except Exception as exc:  # noqa: BLE001
            print(f"daily_report: отчёт по {label} не отправлен: {exc}")
        if day_dir is not None:
            path = day_dir / f"{_safe_label(label)}.txt"
            if path.exists():
                try:
                    _send_document(token, chat_id, path,
                                   caption=f"Переписки {label} · {date_label}")
                    print(f"daily_report: файл {path.name} отправлен")
                except Exception as exc:  # noqa: BLE001
                    print(f"daily_report: файл {path.name} не отправлен: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
