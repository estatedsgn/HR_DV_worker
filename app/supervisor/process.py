"""Управление дочерним процессом автопилота.

Запускает `scripts/run_autopilot.py` как отдельный процесс, стримит его вывод
в кольцевой буфер и в лог-файл, умеет мягко останавливать (CTRL_BREAK на
Windows / SIGTERM на *nix) с жёстким kill в качестве фолбэка.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

ExitCallback = Callable[["ManagedProcess"], Awaitable[None]]


class ManagedProcess:
    def __init__(
        self,
        *,
        name: str,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        log_path: Path,
        ring_size: int = 500,
        on_exit: ExitCallback | None = None,
    ) -> None:
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.log_path = log_path
        self._ring: deque[str] = deque(maxlen=ring_size)
        self._on_exit = on_exit
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stopping = False
        self.started_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def tail(self, lines: int = 25) -> str:
        if not self._ring:
            return "(пока нет вывода)"
        return "\n".join(list(self._ring)[-lines:])

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping = False
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess_new_group_flag()
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            cwd=str(self.cwd),
            env=self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )
        self.started_at = datetime.now(UTC)
        self._reader_task = asyncio.create_task(self._pump_output())

    async def _pump_output(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {self.name} started {self.started_at:%Y-%m-%d %H:%M:%S} pid={self.pid} ===\n")
            log.flush()
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self._ring.append(line)
                log.write(line + "\n")
                log.flush()
        await self._proc.wait()
        if self._on_exit is not None and not self._stopping:
            await self._on_exit(self)

    async def stop(self, *, timeout: float = 12.0) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        self._stopping = True
        proc = self._proc
        # 1) мягкая остановка
        try:
            if sys.platform == "win32":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                proc.send_signal(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # 2) жёсткая
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()


def subprocess_new_group_flag() -> int:
    import subprocess

    return subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
