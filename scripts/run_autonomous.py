"""Автономный раннер всей связки HR_DV_worker.

Держит два долгоживущих процесса и перезапускает упавший, чтобы общение велось
полностью автоматически без присмотра:

  1. Дайвинчик авто-свайпер — лайкает анкеты, ловит взаимные симпатии и заводит
     лида в воронку (durable jsonl + БД), максимум DAIVINCHIK_DAILY_LEAD_LIMIT
     (по умолчанию 7) лидов в сутки.
  2. Автопилот рекрутера — поллит входящие, шлёт первое касание новым лидам и
     ведёт их по воронке (реальная отправка в Telegram).

Запуск:
    .venv\\Scripts\\python.exe scripts\\run_autonomous.py
    :: или двойным кликом start_autonomous.bat

Остановка: Ctrl+C (корректно гасит оба дочерних процесса).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

logger = logging.getLogger("autonomous")

# Restart backoff (seconds) for a crashed child.
RESTART_DELAY = 10.0


def _legacy_specs() -> list[tuple[str, list[str], float]]:
    """Single-account fallback: the original global Дайвинчик + autopilot pair.

    Used when no account in the DB has its own credentials / daivinchik_enabled,
    so the existing single-account (.env) live test keeps running unchanged.
    """
    return [
        ("daivinchik", [PY, "-m", "app.services.daivinchik"], RESTART_DELAY),
        (
            "autopilot",
            [
                PY,
                str(ROOT / "scripts" / "run_autopilot.py"),
                "--all-accounts",
                "--leads-only",
                "--allow-real-send",
            ],
            RESTART_DELAY,
        ),
    ]


def _label(account) -> str:
    return account.telegram_username or account.crmchat_account_id or str(account.id)


def _autopilot_spec(account) -> tuple[str, list[str], float]:
    """A scoped reply-autopilot for one account: its own key, only its dialogs and
    only its outbound jobs (account_id filter) — so accounts never claim each
    other's jobs."""
    return (
        f"autopilot[{_label(account)}]",
        [
            PY,
            str(ROOT / "scripts" / "run_autopilot.py"),
            "--account",
            str(account.id),
            "--leads-only",
            "--allow-real-send",
        ],
        RESTART_DELAY,
    )


def _swiper_spec(account) -> tuple[str, list[str], float]:
    return (
        f"daivinchik[{_label(account)}]",
        [PY, "-m", "app.services.daivinchik", "--account", str(account.id)],
        RESTART_DELAY,
    )


async def _build_process_specs() -> list[tuple[str, list[str], float]]:
    """Per-account processes: a scoped reply-autopilot for every active account
    plus a Дайвинчик swiper for every daivinchik-enabled account.

    Every process is bound to a single account (own CRMchat key, own state file,
    own funnel scope), so accounts run fully in parallel and isolated — no shared
    unscoped worker that could grab another account's jobs. Falls back to the
    legacy single-account pair while no account has the swiper enabled, which
    reproduces the original single-account behaviour exactly.
    """
    try:
        # Imported lazily so the supervisor still starts if the DB is briefly down
        # (it then uses the legacy specs).
        from app.db.session import AsyncSessionLocal
        from app.repositories.account import AccountRepository

        async with AsyncSessionLocal() as session:
            repo = AccountRepository(session)
            active = await repo.list_active(limit=500)
            daivinchik = [a for a in active if a.daivinchik_enabled]
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not load accounts (%s); using legacy single-account setup", exc)
        return _legacy_specs()

    if not daivinchik:
        logger.info("no daivinchik-enabled accounts; using legacy single-account setup")
        return _legacy_specs()

    specs = [_autopilot_spec(account) for account in active]
    specs.extend(_swiper_spec(account) for account in daivinchik)
    logger.info(
        "spawning per-account: %d autopilots + %d swipers",
        len(active),
        len(daivinchik),
    )
    return specs


async def _supervise(name: str, cmd: list[str], restart_delay: float, stop: asyncio.Event) -> None:
    """Запускает один процесс и перезапускает его, пока не попросят остановиться."""
    while not stop.is_set():
        logger.info("[%s] starting: %s", name, " ".join(cmd[1:]))
        # PYTHONUNBUFFERED — чтобы print()-вывод автопилота шёл в общий лог сразу,
        # а не застревал в блочном буфере при перенаправлении в файл.
        child_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ROOT),
            env=child_env,
            stdout=None,  # наследуем stdout/stderr — логи обоих идут в общий вывод
            stderr=None,
        )
        try:
            await proc.wait()
        except asyncio.CancelledError:
            logger.info("[%s] cancel requested; terminating child", name)
            _terminate(proc)
            with contextlib_suppress():
                await asyncio.wait_for(proc.wait(), timeout=15)
            raise
        if stop.is_set():
            logger.info("[%s] stopped (exit=%s)", name, proc.returncode)
            return
        logger.warning(
            "[%s] exited (code=%s); restarting in %.0fs",
            name,
            proc.returncode,
            restart_delay,
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=restart_delay)
        except asyncio.TimeoutError:
            pass


def _terminate(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        pass


class contextlib_suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None


async def _amain() -> None:
    stop = asyncio.Event()

    def _request_stop() -> None:
        logger.info("shutdown signal received; stopping all processes")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Windows ProactorEventLoop не поддерживает add_signal_handler для SIGTERM;
            # KeyboardInterrupt всё равно прилетит как CancelledError ниже.
            pass

    specs = await _build_process_specs()
    tasks = [
        asyncio.create_task(_supervise(name, cmd, delay, stop), name=name)
        for name, cmd, delay in specs
    ]
    logger.info("autonomous runner up: %s", ", ".join(name for name, _, _ in specs))
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("autonomous runner stopped")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # ВАЖНО: на Windows не переключаем на Selector-цикл — он не поддерживает
    # создание дочерних процессов (NotImplementedError). Дефолтный Proactor
    # умеет subprocess; дочерние процессы сами ставят себе нужную политику.
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\nАвтономный раннер остановлен.")


if __name__ == "__main__":
    main()
