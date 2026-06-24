"""Supervisor: управляемый запуск/остановка агента через пульт-адаптер.

Ядро (`Supervisor`) не знает, через что им управляют. Пульт подключается
через `control.base.ControlAdapter` (сейчас реализован Telegram), а инфра
поднимается через сменяемые шаги (`infra.InfraProvider`). Так весь модуль
переносится на VPS без правок ядра — меняется только провайдер инфры.
"""

from app.supervisor.supervisor import Supervisor, SupervisorState

__all__ = ["Supervisor", "SupervisorState"]
