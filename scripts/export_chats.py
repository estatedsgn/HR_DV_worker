"""Выгрузка дневных чатов воронки в читаемые txt-транскрипты.

По каждому аккаунту пишется отдельный файл, подписанный его меткой (Юля/Марго)
и профилем модели (max_only/plus_only) — чтобы ночью сравнить качество A/B и
улучшить промпты. Источник правды — таблица messages в БД.

Запуск:
    .venv\\Scripts\\python.exe scripts\\export_chats.py            # разовый экспорт за сегодня
    .venv\\Scripts\\python.exe scripts\\export_chats.py --days 2    # за последние 2 суток
    .venv\\Scripts\\python.exe scripts\\export_chats.py --loop --interval 180

Файлы: runtime_logs/transcripts/<YYYY-MM-DD>/<label>.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "runtime_logs" / "transcripts"


def _sender(direction: str | None) -> str:
    return "бот " if str(direction or "").lower() == "outbound" else "она "


async def _export_once(days: int) -> int:
    settings = get_settings()
    since = datetime.now(UTC) - timedelta(days=days)
    day_dir = OUT_DIR / datetime.now(UTC).date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    async with AsyncSessionLocal() as session:
        accounts = (
            await session.execute(text("select id, crmchat_account_id from accounts"))
        ).fetchall()
        for acc_id, crmchat_id in accounts:
            label = settings.label_for_account(acc_id, crmchat_id) or str(crmchat_id) or str(acc_id)
            profile = (
                settings.model_profile_for_account(acc_id, crmchat_id) or settings.model_test_profile
            )
            rows = (
                await session.execute(
                    text(
                        """
                        select coalesce(d.telegram_username, d.crmchat_dialog_id) as who,
                               m.direction, m.body, m.created_at, d.id as dialog_id
                        from messages m
                        join dialogs d on d.id = m.dialog_id
                        where d.account_id = :acc and m.created_at >= :since
                        order by who, m.created_at
                        """
                    ),
                    {"acc": acc_id, "since": since},
                )
            ).fetchall()

            lines: list[str] = [
                f"АККАУНТ: {label}   |   МОДЕЛЬ: {profile}   |   аккаунт_id: {acc_id}",
                f"экспорт: {datetime.now(UTC).isoformat(timespec='seconds')}   |   сообщений: {len(rows)}",
                "=" * 88,
                "",
            ]
            current: str | None = None
            for who, direction, body, created_at, _dialog_id in rows:
                if who != current:
                    current = who
                    lines.append("")
                    lines.append(f"───── диалог: {who} ─────")
                stamp = created_at.strftime("%m-%d %H:%M:%S") if created_at else "??"
                content = (body or "").strip() or "[голосовое/медиа]"
                content = content.replace("\n", " ⏎ ")
                lines.append(f"[{stamp}] {_sender(direction)}| {content}")

            safe_label = "".join(c for c in label if c.isalnum() or c in "-_ ").strip() or "account"
            (day_dir / f"{safe_label}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            written += 1
    return written


async def _amain(days: int, loop: bool, interval: int) -> None:
    while True:
        try:
            n = await _export_once(days)
            print(f"[export_chats] wrote {n} transcript file(s) -> {OUT_DIR}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[export_chats] error: {exc}", flush=True)
        if not loop:
            return
        await asyncio.sleep(max(30, interval))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export daily funnel chats to txt transcripts.")
    parser.add_argument("--days", type=int, default=1, help="сколько последних суток включать (по умолч. 1)")
    parser.add_argument("--loop", action="store_true", help="периодически переэкспортировать")
    parser.add_argument("--interval", type=int, default=180, help="секунды между прогонами в --loop")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    asyncio.run(_amain(args.days, args.loop, args.interval))


if __name__ == "__main__":
    main()
