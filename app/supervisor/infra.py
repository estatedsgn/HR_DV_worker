"""Инфра-шаги подъёма агента — сменяемый адаптер.

Каждый шаг шелится наружу через subprocess (docker / alembic / готовые
скрипты проекта), ничего не импортируя из БД в процесс супервизора. Благодаря
этому супервизор живёт на Proactor-цикле (нужен для subprocess на Windows) без
конфликта с asyncpg, а на VPS любой шаг легко отключить (CONTROL_SKIP_*) или
заменить на свой провайдер, не трогая ядро.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings

Progress = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class StepResult:
    ok: bool
    detail: str


async def _run(argv: list[str], *, cwd: Path, timeout: float = 600.0) -> tuple[int, str]:
    """Запустить команду, вернуть (returncode, объединённый вывод)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {argv[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"таймаут {timeout:.0f}s: {' '.join(argv)}"
    return proc.returncode or 0, out.decode("utf-8", errors="replace").strip()


def _tail(text: str, lines: int = 6) -> str:
    rows = [r for r in text.splitlines() if r.strip()]
    return "\n".join(rows[-lines:]) if rows else "(пусто)"


class InfraStep:
    """Базовый шаг. Наследники переопределяют name и run()."""

    name: str = "step"

    async def run(self, progress: Progress) -> StepResult:  # pragma: no cover - интерфейс
        raise NotImplementedError


class DockerPostgresStep(InfraStep):
    name = "Docker + Postgres"

    def __init__(self, settings: Settings, root: Path) -> None:
        self._settings = settings
        self._root = root

    async def run(self, progress: Progress) -> StepResult:
        rc, out = await _run(
            ["docker", "compose", "up", "-d", "postgres"], cwd=self._root, timeout=300
        )
        if rc == 127:
            return StepResult(False, "Docker не найден в PATH — запущен ли Docker Desktop?")
        if rc != 0:
            return StepResult(False, f"docker compose up упал (rc={rc}):\n{_tail(out)}")

        await progress("контейнер поднят, жду готовности Postgres…")
        retries = max(1, self._settings.control_db_health_retries)
        for attempt in range(1, retries + 1):
            rc, out = await _run(
                [
                    "docker", "compose", "exec", "-T", "postgres",
                    "pg_isready", "-U", "hr_dv_worker", "-d", "hr_dv_worker",
                ],
                cwd=self._root,
                timeout=20,
            )
            if rc == 0:
                return StepResult(True, f"Postgres готов (попыток: {attempt})")
            await asyncio.sleep(2)
        return StepResult(False, f"Postgres не ответил за {retries} попыток:\n{_tail(out)}")


class MigrationsStep(InfraStep):
    name = "Миграции (alembic)"

    def __init__(self, root: Path) -> None:
        self._root = root

    async def run(self, progress: Progress) -> StepResult:
        rc, out = await _run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=self._root,
            timeout=300,
        )
        if rc != 0:
            return StepResult(False, f"alembic upgrade head упал (rc={rc}):\n{_tail(out)}")
        return StepResult(True, "схема БД на head")


class CRMChatStep(InfraStep):
    name = "Подключение к CRMChat"

    _RE = re.compile(r"active=(\d+).*?healthy=(\d+)", re.DOTALL)

    def __init__(self, root: Path) -> None:
        self._root = root

    async def run(self, progress: Progress) -> StepResult:
        rc, out = await _run(
            [sys.executable, "scripts/sync_crmchat_accounts.py", "--report"],
            cwd=self._root,
            timeout=120,
        )
        if rc != 0:
            return StepResult(False, f"проверка CRMChat упала (rc={rc}):\n{_tail(out)}")
        match = self._RE.search(out)
        if not match:
            return StepResult(False, f"не разобрал отчёт CRMChat:\n{_tail(out)}")
        active, healthy = int(match.group(1)), int(match.group(2))
        if active < 1:
            return StepResult(False, "нет активных Telegram-аккаунтов в CRMChat — подключить аккаунт")
        if healthy < 1:
            return StepResult(False, f"аккаунты есть (active={active}), но ни один не healthy")
        return StepResult(True, f"CRMChat ок: active={active}, healthy={healthy}")


class InfraProvider:
    """Отдаёт упорядоченный список шагов подъёма. На VPS подменяется целиком."""

    def steps(self) -> list[InfraStep]:  # pragma: no cover - интерфейс
        raise NotImplementedError


class LocalInfraProvider(InfraProvider):
    def __init__(self, settings: Settings, root: Path) -> None:
        self._settings = settings
        self._root = root

    def steps(self) -> list[InfraStep]:
        steps: list[InfraStep] = []
        if not self._settings.control_skip_docker:
            steps.append(DockerPostgresStep(self._settings, self._root))
        if not self._settings.control_skip_migrations:
            steps.append(MigrationsStep(self._root))
        steps.append(CRMChatStep(self._root))
        return steps
