"""Точка входа пульта.

Локально:  .venv\\Scripts\\python.exe -m app.supervisor
На VPS:    python -m app.supervisor   (тот же код, см. infra/CONTROL_SKIP_* для подмены шагов)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.core.config import get_settings
from app.supervisor.control.telegram import TelegramControlAdapter
from app.supervisor.infra import LocalInfraProvider
from app.supervisor.supervisor import Supervisor

ROOT = Path(__file__).resolve().parents[2]


async def amain() -> None:
    settings = get_settings()
    token = settings.control_bot_token
    if not token:
        print("CONTROL_BOT_TOKEN не задан в .env — пульт не запустить.")
        raise SystemExit(2)

    infra = LocalInfraProvider(settings, ROOT)
    supervisor = Supervisor(settings=settings, infra=infra, root=ROOT)
    adapter = TelegramControlAdapter(
        supervisor,
        token=token,
        admin_chat_id=settings.control_admin_chat_id,
        root=ROOT,
        autostart=settings.control_autostart,
    )
    print("🎛 Пульт запущен. Открой бота в Telegram и нажми /start.")
    await adapter.run()


def main() -> None:
    # Консоль Windows по умолчанию cp1251 и падает на эмодзи — форсим UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    # ВАЖНО: НЕ ставим WindowsSelectorEventLoopPolicy — нужен Proactor для
    # дочерних процессов (docker/alembic/autopilot). httpx работает на нём.
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nПульт остановлен.")


if __name__ == "__main__":
    main()
