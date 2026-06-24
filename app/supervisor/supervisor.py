"""Ядро супервизора: машина состояний запуска/остановки агента.

Транспортно-независимо. Пульт (Telegram и т.п.) подключается через
`set_notifier` (получать события) и вызывает `start()` / `stop()` / `status_text()`.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from app.core.config import Settings
from app.supervisor.infra import InfraProvider
from app.supervisor.process import ManagedProcess

Notifier = Callable[[str], Awaitable[None]]


class SupervisorState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


_EMOJI = {
    SupervisorState.STOPPED: "⚪️ остановлен",
    SupervisorState.STARTING: "🟡 запускается",
    SupervisorState.RUNNING: "🟢 работает",
    SupervisorState.STOPPING: "🟡 останавливается",
    SupervisorState.ERROR: "🔴 ошибка",
}


class Supervisor:
    def __init__(
        self,
        *,
        settings: Settings,
        infra: InfraProvider,
        root: Path,
    ) -> None:
        self._settings = settings
        self._infra = infra
        self._root = root
        self._notifier: Notifier | None = None
        self._lock = asyncio.Lock()
        self.state = SupervisorState.STOPPED
        self.last_error: str | None = None
        self._autopilot: ManagedProcess | None = None
        self._started_at: datetime | None = None
        # Авто-рестарт автопилота: считаем падения в пределах окна, чтобы
        # самовосстанавливаться от разовых падений, но не уходить в горячий
        # цикл, если автопилот падает сразу при старте.
        self._crash_count = 0
        self._last_crash_at = 0.0
        self._restart_window_seconds = 600.0  # «серия» падений сбрасывается через 10 мин аптайма
        self._restart_base_backoff = 5.0
        self._restart_max_backoff = 120.0
        self._max_restarts_in_window = 6

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    async def _emit(self, text: str) -> None:
        if self._notifier is not None:
            try:
                await self._notifier(text)
            except Exception:  # noqa: BLE001 - пульт не должен ронять ядро
                pass

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        if self.state in (SupervisorState.STARTING, SupervisorState.STOPPING):
            await self._emit("⏳ уже в процессе, подожди завершения текущей операции")
            return
        if self.state == SupervisorState.RUNNING and self._autopilot and self._autopilot.is_running:
            await self._emit("ℹ️ агент уже работает")
            return

        async with self._lock:
            self.state = SupervisorState.STARTING
            self.last_error = None
            await self._emit("🟡 Запускаю агента…")

            for step in self._infra.steps():
                await self._emit(f"▸ {step.name}…")

                async def progress(msg: str, _name=step.name) -> None:
                    await self._emit(f"   {_name}: {msg}")

                try:
                    result = await step.run(progress)
                except Exception as exc:  # noqa: BLE001
                    result = type("R", (), {"ok": False, "detail": f"{type(exc).__name__}: {exc}"})()
                if not result.ok:
                    self.state = SupervisorState.ERROR
                    self.last_error = f"{step.name}: {result.detail}"
                    await self._emit(f"🔴 Шаг «{step.name}» не прошёл:\n{result.detail}\n\nАгент НЕ запущен.")
                    return
                await self._emit(f"✅ {step.name}: {result.detail}")

            await self._launch_autopilot()
            self.state = SupervisorState.RUNNING
            self._started_at = datetime.now(UTC)
            target = self._settings.control_target_username or "все диалоги"
            await self._emit(
                f"🟢 Агент запущен и работает (цель: {target}).\n"
                f"Будет крутиться, пока не нажмёшь ⏹ Стоп."
            )

    async def _launch_autopilot(self) -> None:
        # -u + PYTHONUNBUFFERED: иначе stdout автопилота блочно буферизуется и
        # строки циклов/ошибок попадают в лог с задержкой в минуты — отладка
        # падений (и хвост лога в авто-рестарте) становится бесполезной.
        argv = [sys.executable, "-u", "scripts/run_autopilot.py"]
        argv += shlex.split(self._settings.control_autopilot_args)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        target = self._settings.control_target_username
        if target:
            argv += ["--only-username", target]
            env["OUTBOUND_ALLOWED_USERNAMES"] = target
        log_path = self._root / "runtime_logs" / "supervisor_autopilot.log"
        self._autopilot = ManagedProcess(
            name="autopilot",
            argv=argv,
            cwd=self._root,
            env=env,
            log_path=log_path,
            on_exit=self._on_autopilot_exit,
        )
        await self._autopilot.start()

    async def _on_autopilot_exit(self, proc: ManagedProcess) -> None:
        # Сработает, только если процесс умер сам (не по нашей команде stop).
        if self.state != SupervisorState.RUNNING:
            return
        rc = proc.returncode
        tail = proc.tail(15)

        now = time.monotonic()
        # Если предыдущее падение было давно — автопилот успел нормально
        # поработать, значит это новая «серия», счётчик сбрасываем.
        if now - self._last_crash_at > self._restart_window_seconds:
            self._crash_count = 0
        self._crash_count += 1
        self._last_crash_at = now

        # Предохранитель: если автопилот падает раз за разом (например, битый
        # конфиг), не крутим бесконечный рестарт — паркуемся в ERROR.
        if self._crash_count > self._max_restarts_in_window:
            self.state = SupervisorState.ERROR
            self.last_error = (
                f"автопилот падает повторно (rc={rc}); авто-рестарт остановлен "
                f"после {self._crash_count - 1} попыток"
            )
            await self._emit(
                f"🔴 Автопилот упал {self._crash_count} раз подряд (rc={rc}). "
                f"Авто-рестарт остановлен — нужна ручная проверка.\n"
                f"Последние строки лога:\n{tail}\n\nНажми ▶️ Старт после фикса."
            )
            return

        backoff = min(
            self._restart_base_backoff * (2 ** (self._crash_count - 1)),
            self._restart_max_backoff,
        )
        await self._emit(
            f"🔁 Автопилот упал (rc={rc}), авто-рестарт #{self._crash_count} "
            f"через {backoff:.0f}с…\nПоследние строки лога:\n{tail}"
        )
        await asyncio.sleep(backoff)

        # Пока ждали — могли нажать ⏹ Стоп или поднять автопилот вручную.
        async with self._lock:
            if self.state != SupervisorState.RUNNING:
                return
            if self._autopilot is not None and self._autopilot.is_running:
                return
            try:
                await self._launch_autopilot()
            except Exception as exc:  # noqa: BLE001
                self.state = SupervisorState.ERROR
                self.last_error = f"авто-рестарт не удался: {type(exc).__name__}: {exc}"
                await self._emit(f"🔴 Авто-рестарт автопилота не удался: {exc}")
                return
        await self._emit("🟢 Автопилот перезапущен автоматически.")

    # ------------------------------------------------------------------- stop
    async def stop(self) -> None:
        if self.state == SupervisorState.STOPPED:
            await self._emit("ℹ️ агент уже остановлен")
            return
        if self.state == SupervisorState.STOPPING:
            await self._emit("⏳ уже останавливаю…")
            return
        async with self._lock:
            self.state = SupervisorState.STOPPING
            await self._emit("🟡 Останавливаю агента…")
            if self._autopilot is not None:
                await self._autopilot.stop()
            self._autopilot = None
            self._started_at = None
            self.state = SupervisorState.STOPPED
            await self._emit("⚪️ Агент остановлен. Инфра (Docker/Postgres) оставлена поднятой.")

    # ----------------------------------------------------------------- status
    def status_text(self) -> str:
        lines = [f"Состояние: {_EMOJI[self.state]}"]
        if self.state == SupervisorState.RUNNING and self._started_at:
            uptime = datetime.now(UTC) - self._started_at
            mins = int(uptime.total_seconds() // 60)
            lines.append(f"Аптайм: {mins} мин")
            if self._autopilot:
                lines.append(f"PID автопилота: {self._autopilot.pid}")
        target = self._settings.control_target_username or "все диалоги"
        lines.append(f"Цель: {target}")
        if self.last_error:
            lines.append(f"Последняя ошибка: {self.last_error}")
        return "\n".join(lines)

    def logs_text(self, lines: int = 20) -> str:
        if self._autopilot is None:
            return "Автопилот не запущен — логов нет."
        return self._autopilot.tail(lines)

    async def shutdown(self) -> None:
        """Аккуратно погасить дочерний процесс при выходе из пульта."""
        if self._autopilot is not None and self._autopilot.is_running:
            await self._autopilot.stop()
