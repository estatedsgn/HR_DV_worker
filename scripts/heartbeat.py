"""Проверка здоровья воркера + уведомление в управляющий чат.

Раньше heartbeat подтверждал только что *процессы запущены* (сервисы active,
Postgres up, RAM есть) и всегда слал «✅ жив» — даже когда реальная работа стояла
(поллинг встал, аккаунт во flood-wait, очередь забита ошибками, автопилот умер).
Из-за этого приходило «всё ок», когда всё было не ок.

Теперь скрипт делает **функциональные** проверки (идёт ли поллинг, здоровы ли
аккаунты, не копятся ли ошибки отправки, жив ли автопилот) и шлёт честный вердикт:

    ‼️ ЕСТЬ ПРОБЛЕМА     — что-то реально не работает (severity=FAIL)
    ⚠️ есть предупреждения — работает, но есть на что посмотреть (severity=WARN)
    ✅ всё работает нормально — функциональные проверки прошли

Режимы запуска:
  * без флагов            — всегда шлёт сводку (ежечасный «жив/сводка»).
  * --quiet-if-healthy    — шлёт ТОЛЬКО при проблеме (FAIL/WARN); при «всё ок»
                            молчит. Для частого вотчдога (раз в ~10 мин), чтобы
                            проблема прилетала сразу, а не раз в час.

Никогда не падает с ненулевым кодом из-за сети/конфига — чтобы systemd-таймер не
спамил ошибками; печатает причину в журнал и выходит 0.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent

# Severity levels (выше — хуже).
OK, WARN, FAIL = 0, 1, 2

# Если поллинг не завершал прогон дольше этого — считаем, что он встал.
# Нормальный цикл ~3.5 мин под троттлингом, поэтому порог с запасом.
POLL_STALE_MINUTES = 20
# Сколько джоб в очереди «зависло», если висят дольше этого.
OUTBOUND_STUCK_MINUTES = 15
LOW_RAM_MB = 1536


def _svc_active(name: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return out or "unknown"
    except Exception:  # noqa: BLE001 - heartbeat must never crash
        return "unknown"


def _process_alive(pattern: str) -> bool:
    try:
        rc = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=10,
        ).returncode
        return rc == 0
    except Exception:  # noqa: BLE001
        return False


def _postgres_running() -> bool:
    try:
        out = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=20,
        ).stdout
        return "postgres" in out.split()
    except Exception:  # noqa: BLE001
        return False


def _mem_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def _mark(sev: int) -> str:
    return {OK: "✅", WARN: "⚠️", FAIL: "❌"}[sev]


def _fmt_ago(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "никогда"
    delta = now - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    return f"{mins // 60}ч {mins % 60}м назад"


async def _db_checks(in_hours: bool) -> list[tuple[int, str]]:
    """Функциональные проверки по БД. Возвращает список (severity, текст).

    Любая ошибка доступа к БД — это сама по себе FAIL. Вне рабочих часов
    (in_hours=False) автопилот намеренно молчит: пауза поллинга и ждущая
    очередь — это норма, не повод для тревоги.
    """
    from sqlalchemy import text

    from app.db.session import AsyncSessionLocal

    findings: list[tuple[int, str]] = []
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as s:
            # --- поллинг: свежесть и статус последнего прогона ----------------
            row = (
                await s.execute(
                    text(
                        "select status, error_message, finished_at, started_at "
                        "from telegram_polling_runs order by created_at desc limit 1"
                    )
                )
            ).first()
            if not in_hours:
                findings.append((OK, "поллинг: пауза (вне рабочих часов)"))
            elif row is None:
                findings.append((WARN, "поллинг: прогонов ещё не было"))
            else:
                status, err, finished_at, started_at = row
                last = finished_at or started_at
                age_min = (now - last).total_seconds() / 60 if last else 1e9
                if err or status not in ("completed", "running"):
                    findings.append(
                        (FAIL, f"поллинг: последний прогон '{status}'"
                               + (f" ({err})" if err else ""))
                    )
                elif finished_at is None and age_min > POLL_STALE_MINUTES:
                    findings.append(
                        (FAIL, f"поллинг: прогон висит {age_min:.0f} мин без завершения")
                    )
                elif age_min > POLL_STALE_MINUTES:
                    findings.append(
                        (FAIL, f"поллинг: не было прогонов {age_min:.0f} мин — похоже, встал")
                    )
                else:
                    findings.append(
                        (OK, f"поллинг: ок ({_fmt_ago(last, now)})")
                    )

            # --- аккаунты: flood-wait / health ------------------------------
            accs = (
                await s.execute(
                    text(
                        "select coalesce(telegram_username, crmchat_account_id) name, "
                        "status, health_status, flood_wait_until "
                        "from accounts"
                    )
                )
            ).all()
            if not accs:
                findings.append((FAIL, "аккаунтов в БД нет — слать некому"))
            else:
                bad = []
                for name, status, health, flood_until in accs:
                    if flood_until and flood_until > now:
                        bad.append((WARN, f"{name}: flood-wait до {flood_until:%H:%M}"))
                    elif health and health != "healthy":
                        bad.append((WARN, f"{name}: health={health}"))
                    elif status and status not in ("active", None):
                        bad.append((WARN, f"{name}: status={status}"))
                if bad:
                    findings.extend(bad)
                else:
                    findings.append((OK, f"аккаунты: {len(accs)} здоровы"))

            # --- очередь отправки: ошибки и зависшие -------------------------
            stuck_cut = now - timedelta(minutes=OUTBOUND_STUCK_MINUTES)
            hour_cut = now - timedelta(hours=1)
            failed_1h = (
                await s.execute(
                    text("select count(*) from outbound_jobs "
                         "where status='failed' and updated_at > :c"),
                    {"c": hour_cut},
                )
            ).scalar() or 0
            stuck = (
                await s.execute(
                    text("select count(*) from outbound_jobs "
                         "where status in ('queued','in_progress','pending') "
                         "and created_at < :c"),
                    {"c": stuck_cut},
                )
            ).scalar() or 0
            if failed_1h:
                findings.append((WARN, f"очередь: {failed_1h} отправок failed за час"))
            # Вне рабочих часов джобы законно ждут открытия окна — не тревога.
            if stuck and in_hours:
                findings.append(
                    (WARN, f"очередь: {stuck} джоб висят >{OUTBOUND_STUCK_MINUTES} мин")
                )
            elif stuck:
                findings.append(
                    (OK, f"очередь: {stuck} джоб ждут начала рабочих часов")
                )
            if not failed_1h and not stuck:
                findings.append((OK, "очередь отправки: чисто"))

            # --- активность (информационно, не повод для тревоги) -----------
            act = (
                await s.execute(
                    text(
                        "select "
                        "count(*) filter (where direction='inbound' and created_at>:h) in1, "
                        "count(*) filter (where direction='outbound' and created_at>:h) out1, "
                        "max(created_at) filter (where direction='inbound') last_in, "
                        "max(created_at) filter (where direction='outbound') last_out "
                        "from messages"
                    ),
                    {"h": hour_cut},
                )
            ).first()
            if act is not None:
                in1, out1, last_in, last_out = act
                findings.append(
                    (OK, f"ℹ️ за час: вход {in1} / исход {out1}; "
                         f"посл. вход {_fmt_ago(last_in, now)}, "
                         f"исход {_fmt_ago(last_out, now)}")
                )
    except Exception as exc:  # noqa: BLE001
        findings.append((FAIL, f"БД недоступна: {type(exc).__name__}: {exc}"))
    return findings


def _gather_findings() -> tuple[list[tuple[int, str]], dict]:
    s = get_settings()
    tz = ZoneInfo(s.daivinchik_timezone)
    now = datetime.now(tz)
    in_hours = s.daivinchik_active_hours_start <= now.hour < s.daivinchik_active_hours_end

    findings: list[tuple[int, str]] = []

    # --- инфраструктура -------------------------------------------------------
    sup = _svc_active("hrdv-supervisor")
    findings.append((OK if sup == "active" else FAIL, f"пульт-супервизор: {sup}"))

    dai = _svc_active("hrdv-daivinchik")
    findings.append((OK if dai == "active" else FAIL, f"свайпер Дайвинчика: {dai}"))

    pg = _postgres_running()
    findings.append((OK if pg else FAIL, f"Postgres: {'up' if pg else 'DOWN'}"))

    # автопилот — дочерний процесс супервизора; сервис active не гарантирует,
    # что сам автопилот жив. Если его нет — WARN (мог быть ⏹ Стоп вручную).
    autopilot = _process_alive("run_autopilot.py")
    findings.append(
        (OK, "автопилот воронки: работает") if autopilot
        else (WARN, "автопилот воронки НЕ запущен — если не нажимали ⏹, жмите ▶️ Старт")
    )

    mem = _mem_available_mb()
    if mem is not None:
        findings.append(
            (WARN, f"RAM свободно {mem} МБ (<1.5 ГБ — пора апать тариф)")
            if mem < LOW_RAM_MB else (OK, f"RAM свободно {mem} МБ")
        )

    meta = {"now": now, "in_hours": in_hours, "settings": s}
    return findings, meta


def build_report(quiet_if_healthy: bool) -> tuple[str | None, int]:
    """Собирает текст отчёта и worst-severity. text=None → отправлять не нужно."""
    findings, meta = _gather_findings()
    try:
        findings += asyncio.run(_db_checks(in_hours=meta["in_hours"]))
    except Exception as exc:  # noqa: BLE001
        findings.append((FAIL, f"проверки БД упали: {type(exc).__name__}: {exc}"))

    worst = max((sev for sev, _ in findings), default=OK)

    if quiet_if_healthy and worst == OK:
        return None, worst

    now = meta["now"]
    s = meta["settings"]
    header = {
        FAIL: "‼️ HR_DV_worker — ЕСТЬ ПРОБЛЕМА",
        WARN: "⚠️ HR_DV_worker — работает, но есть предупреждения",
        OK: "✅ HR_DV_worker — всё работает нормально",
    }[worst]

    lines = [f"{header} — {now:%H:%M} {now.tzname()}"]
    # Сначала проблемы (FAIL, потом WARN), затем ок-строки.
    for sev in (FAIL, WARN, OK):
        for fs, txt in findings:
            if fs == sev:
                lines.append(f"{_mark(sev)} {txt}")
    window = "активно" if meta["in_hours"] else "тихий час"
    lines.append(
        f"🕒 окно {s.daivinchik_active_hours_start:02d}–"
        f"{s.daivinchik_active_hours_end:02d}: {window}"
    )
    return "\n".join(lines), worst


def main() -> int:
    parser = argparse.ArgumentParser(description="HR_DV_worker heartbeat / watchdog")
    parser.add_argument(
        "--quiet-if-healthy",
        action="store_true",
        help="Слать сообщение только при проблеме (для частого вотчдога).",
    )
    parser.add_argument(
        "--always",
        action="store_true",
        help="Слать почасовую сводку даже вне рабочих часов (для ручной проверки).",
    )
    args = parser.parse_args()

    s = get_settings()
    token = s.control_bot_token
    chat_id = s.control_admin_chat_id
    if not token or not chat_id:
        print("heartbeat: CONTROL_BOT_TOKEN/CONTROL_ADMIN_CHAT_ID не заданы — пропуск")
        return 0

    # Почасовую сводку «жив/ок» шлём только в рабочие часы (10–22). Вне окна
    # она не нужна — поломки всё равно ловит вотчдог (--quiet-if-healthy, 24/7).
    if not args.quiet_if_healthy and not args.always:
        tz = ZoneInfo(s.daivinchik_timezone)
        now = datetime.now(tz)
        in_hours = s.daivinchik_active_hours_start <= now.hour < s.daivinchik_active_hours_end
        if not in_hours:
            print(f"heartbeat: {now:%H:%M} вне рабочих часов — почасовую сводку не шлю")
            return 0

    text_out, worst = build_report(quiet_if_healthy=args.quiet_if_healthy)
    if text_out is None:
        print(f"heartbeat: всё ок (severity={worst}), --quiet-if-healthy — молчу")
        return 0

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text_out},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"heartbeat: отправка не удалась: {exc}")
        return 0
    print(f"heartbeat: отправлено (severity={worst})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
