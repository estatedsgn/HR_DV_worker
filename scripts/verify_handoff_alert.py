"""Реальная проверка хендофф-уведомления: собирает лида с заполненным профилем
на стадии human_handoff и шлёт карточку кандидата в настроенный бот.

Использует тот же путь, что и боевая воронка (LeadNotifier.notify_handoff,
читающий .env: HANDOFF_BOT_TOKEN/HANDOFF_CHAT_ID либо фолбэк на CONTROL_BOT_TOKEN/
CONTROL_ADMIN_CHAT_ID). Если кредов нет — печатает карточку в консоль (dry-run).

Запуск:  .venv\\Scripts\\python.exe scripts\\verify_handoff_alert.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.services.lead_notifier import LeadNotifier  # noqa: E402


# Новый тестовый лид, как будто воронка его довела до хендоффа.
SAMPLE_PROFILE = {
    "candidate_name": "Аня",
    "phone_number": "+7 999 123-45-67",
    "age": 19,
    "interview_day_confirmed": True,
    "interview_time": "14:00",
    "phone_model": "iPhone 13",
    "room_available": True,
    "profile_info": "учусь в универе, в свободное время рисую и снимаю тикток",
}


async def main() -> None:
    settings = get_settings()
    notifier = LeadNotifier(settings)

    print("=== Карточка хендоффа (что уйдёт в бот) ===")
    for label, value in LeadNotifier.build_handoff_card(SAMPLE_PROFILE):
        print(f"  • {label}: {value}")
    print()

    if not notifier.handoff_enabled:
        print(
            "handoff_enabled = False — нет кредов бота.\n"
            "Заполни CONTROL_ADMIN_CHAT_ID (или HANDOFF_CHAT_ID) в .env и повтори.\n"
            "chat_id берётся из getUpdates после /start боту."
        )
        return

    await notifier.notify_handoff(
        telegram_username="@test_handoff_lead",
        account_label="HR recruiting",
        profile=SAMPLE_PROFILE,
        reason="funnel_requested_handoff",
    )
    token_tail = (notifier._handoff_token or "")[-6:]
    print(
        f"Отправлено в бот (token …{token_tail}, chat_id={notifier._handoff_chat_id}). "
        "Проверь Telegram."
    )


if __name__ == "__main__":
    asyncio.run(main())
