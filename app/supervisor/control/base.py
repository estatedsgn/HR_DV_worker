"""Интерфейс пульта управления.

Любой транспорт (Telegram, web, CLI) реализует `ControlAdapter`: умеет
слать уведомления админу (`notify`) и крутит свой цикл приёма команд (`run`),
дёргая `supervisor.start/stop/status_text`. Ядро о транспорте не знает.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.supervisor.supervisor import Supervisor


class ControlAdapter(ABC):
    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor
        supervisor.set_notifier(self.notify)

    @abstractmethod
    async def notify(self, text: str) -> None:
        """Отправить сообщение/уведомление владельцу пульта."""

    @abstractmethod
    async def run(self) -> None:
        """Запустить цикл приёма команд (блокирующий до остановки пульта)."""
