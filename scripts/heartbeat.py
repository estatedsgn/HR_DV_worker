"""Ежечасный heartbeat: подтверждает в управляющий чат, что воркер жив.

Запускается systemd-таймером раз в час (`hrdv-heartbeat.timer`). Шлёт короткую
сводку в Telegram-чат супервизора (`CONTROL_BOT_TOKEN` + `CONTROL_ADMIN_CHAT_ID`):
состояние сервисов, Postgres, свободная RAM и активны ли рабочие часы.

Никогда не падает с ненулевым кодом из-за сети/конфига — чтобы таймер не спамил
systemd ошибками; вместо этого печатает причину в журнал и выходит 0.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent


def _svc_active(name: str) -> str:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return out or "unknown"
    except Exception:  # noqa: BLE001 - heartbeat must never crash
        return "unknown"


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


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def main() -> int:
    s = get_settings()
    token = s.control_bot_token
    chat_id = s.control_admin_chat_id
    if not token or not chat_id:
        print("heartbeat: CONTROL_BOT_TOKEN/CONTROL_ADMIN_CHAT_ID не заданы — пропуск")
        return 0

    tz = ZoneInfo(s.daivinchik_timezone)
    now = datetime.now(tz)
    in_hours = s.daivinchik_active_hours_start <= now.hour < s.daivinchik_active_hours_end

    sup = _svc_active("hrdv-supervisor")
    dai = _svc_active("hrdv-daivinchik")
    pg = _postgres_running()
    mem = _mem_available_mb()

    lines = [
        f"🫀 HR_DV_worker жив — {now:%H:%M} {now.tzname()}",
        f"{_mark(sup == 'active')} пульт-супервизор: {sup}",
        f"{_mark(dai == 'active')} свайпер Дайвинчика: {dai}",
        f"{_mark(pg)} Postgres: {'up' if pg else 'down'}",
        f"🕒 окно {s.daivinchik_active_hours_start:02d}–{s.daivinchik_active_hours_end:02d}: "
        f"{'активно' if in_hours else 'тихий час'}",
    ]
    if mem is not None:
        lines.append(f"🧠 свободно RAM: {mem} МБ")
        if mem < 1536:
            lines.append("⚠️ мало памяти (<1.5 ГБ) — пора подумать об апгрейде тарифа")
    text = "\n".join(lines)

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"heartbeat: отправка не удалась: {exc}")
        return 0
    print("heartbeat: отправлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
